"""The Pulse engine for one Talk runtime: memory extraction, follow-up checks and model selection.

The model steps run here, never inside a ledger lock: read a snapshot, ask the model through
pulse_model (the host's ctx.llm), then apply the validated result under the account's lock.
A background worker (started by server.py) runs each known account after a call ends and on a
slow tick, the way upstream's Pulse tick reads chats once they go quiet; checks and gates call
run_once/select directly. Every step records how it was decided ("model" or "fallback" with the
reason), so deterministic ranking is never reported as model selection.
"""
import logging
import threading

from . import pulse, pulse_followup, pulse_memory, pulse_select
from .pulse_model import ModelUnavailable

logger = logging.getLogger("tg-voice")

TICK_S = 300.0
START_DELAY_S = 30.0  # the worker (and plugin discovery) starts after the server is already answering
SELECT_ON_CALL_S = 8.0  # bound on a model selection made while a call is being set up
MAX_SOURCES = 12  # sessions read per account per run
MAX_CHECKS_PER_RUN = 6


class PulseEngine:
    def __init__(self, runtime, model):
        self.rt = runtime
        self.model = model
        self.lock = threading.Lock()  # one engine run at a time per runtime
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.users = set()
        self.thread = None
        self.last = {}  # user id -> summary of the last run, for logs and gates

    # --- background worker -------------------------------------------------------------------------
    def known_users(self):
        """Users of this profile and bot with a Pulse record, so a restarted server resumes their follow-ups."""
        prefix = f"hermes_talk:pulse:{self.rt.account(0).key[:-1]}"
        users = set()
        for key, _ in self.rt.history.list_meta_prefix(prefix):
            tail = key[len(prefix):]
            if tail.isdigit():
                users.add(int(tail))
        return users

    def start(self, loader=None, delay=START_DELAY_S):
        """Start the worker. After `delay` it runs `loader` (server.py: load the plugin's ctx.llm through plugin
        discovery) off the request path, then resumes every account with a Pulse record without waiting a tick.
        Until the route is bound, call setup selects with the labelled fallback."""
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, args=(loader, delay), name="talk-pulse-engine",
                                           daemon=True)
            self.thread.start()

    def stop(self):
        self.stopped.set()
        self.wake.set()

    def poke(self, user_id):
        self.users.add(user_id)
        self.wake.set()

    def _loop(self, loader, delay):
        if self.stopped.wait(delay):
            return
        if loader is not None:
            try:
                route = loader()
            except Exception as exc:  # no route: every step keeps degrading visibly
                route = None
                logger.warning("pulse model route could not be loaded (%s)", type(exc).__name__)
            logger.info("pulse model route: %s", "plugin_llm:hermes-talk" if route is not None else "none (fallback)")
        try:
            self.users |= self.known_users()
        except Exception as exc:  # the worker still runs for accounts whose calls end from now on
            logger.warning("pulse engine could not list accounts (%s)", type(exc).__name__)
        self.wake.set()  # first run right away: due follow-ups do not wait a whole tick after a restart
        while not self.stopped.is_set():
            self.wake.wait(TICK_S)
            self.wake.clear()
            if self.stopped.is_set():
                return
            for user_id in sorted(self.users.copy()):  # request threads add users while this runs
                try:
                    self.run_once(user_id)
                except Exception as exc:  # one account's failure never stops the others
                    logger.warning("pulse engine run failed (%s)", type(exc).__name__)

    # --- one run -------------------------------------------------------------------------------------
    def run_once(self, user_id):
        """Extract memory from new lines, decay, run due follow-up checks, then refresh the selection."""
        with self.lock:
            account = self.rt.account(user_id)
            self.rt.pulse_refresh(user_id)
            summary = {"memory": self._extract(account), "followups": self._followups(account)}
            summary["selection"] = self.select(account)
            self.last[user_id] = summary
            logger.info("pulse engine run %s", {k: v for k, v in summary.items() if k != "memory"})
            return summary

    def _sources(self, state):
        talk = [(sid, "talk") for sid in state["memory"]["talk"][-4:]]
        seen = set()
        cards = sorted((c for c in state["items"].values() if c.get("kind", "activity") == "activity"),
                       key=lambda c: -c["at"])
        profile = []
        for card in cards:
            if card["ref"] not in seen:
                seen.add(card["ref"])
                profile.append((card["ref"], card["source"]))
        return (talk + profile)[:MAX_SOURCES]

    def _extract(self, account):
        now = self.rt.clock()
        state = self.rt.pulse.snapshot(account)
        out = {"read": 0, "kept": 0, "rejected": 0, "closed": 0, "by": "model", "errors": 0}
        if not self.model.available():
            out["by"] = "unavailable"
            return out
        for session_id, source in self._sources(state):
            turns = self.rt.history.recent(session_id, pulse_memory.MAX_ROWS)
            mark = state["memory"]["read"].get(session_id)
            reminders = [c for c in state["items"].values() if c.get("kind") == "followup"]
            rows = pulse_memory.new_rows(turns, mark, lambda role, text, cards=reminders: role == "assistant" and any(
                pulse.mentioned(text, card, []) for card in cards))
            if not rows:
                continue
            remembered = pulse_memory.remembered(state["memory"])
            try:
                items, closed, rejected = pulse_memory.extract(self.model, rows, remembered, now)
            except ModelUnavailable:
                out["errors"] += 1
                continue  # the read mark stays, so these lines are read again next run
            last = [t for t in turns if t[0] in ("user", "assistant") and str(t[1]).strip()][-1]
            provenance = {"session": session_id, "source": source}

            def change(st, items=items, closed=closed, provenance=provenance, last=last, sid=session_id):
                touched = pulse_memory.apply(st["memory"], items, closed, provenance, now)
                st["memory"]["read"][sid] = pulse_memory.turn_key(*last)
                pulse_followup.schedule(st["memory"])
                return touched

            self.rt.pulse.update(account, change)
            state = self.rt.pulse.snapshot(account)
            out["read"] += len(rows)
            out["kept"] += len(items)
            out["closed"] += len(closed)
            out["rejected"] += rejected
        out["faded"] = self.rt.pulse.update(account, lambda st: pulse_memory.decay(st["memory"], now))
        return out

    def _lines_since(self, session_id, evidence):
        turns = [(r, t) for r, t in self.rt.history.recent(session_id, pulse_memory.MAX_ROWS)
                 if r in ("user", "assistant")]
        wanted = pulse_memory.norm(evidence)
        start = next((i + 1 for i in range(len(turns) - 1, -1, -1) if wanted in pulse_memory.norm(turns[i][1])), 0)
        return [f"{'user' if r == 'user' else 'Hermes'}: {pulse_memory._clip(t, 600)}" for r, t in turns[start:]]

    def _followups(self, account):
        now = self.rt.clock()
        out = {"checked": 0, "told": 0, "closed": 0, "waiting": 0, "gave_up": 0, "failed": 0}
        state = self.rt.pulse.snapshot(account)
        pulse_followup.schedule(state["memory"])
        due = pulse_followup.due_items(state["memory"], now)[:MAX_CHECKS_PER_RUN]
        talk = state["memory"]["talk"][-1:] if state["memory"]["talk"] else []
        voice = []
        for sid in talk:
            voice += [f"{'user' if r == 'user' else 'Hermes'}: {pulse_memory._clip(t, 600)}"
                      for r, t in self.rt.history.recent(sid, 30)]
        for item in due:
            said = item["provenance"][0] if item["provenance"] else {}
            since = self._lines_since(said.get("session"), item["evidence"]) if said.get("session") else []
            if not self.model.available():
                out["failed"] += 1
                self.rt.pulse.update(account, lambda st, i=item: self._failed(st, i["id"], now))
                continue
            try:
                result = pulse_followup.check(self.model, item, pulse_followup.material(item, since, voice, now))
            except ModelUnavailable:
                out["failed"] += 1
                self.rt.pulse.update(account, lambda st, i=item: self._failed(st, i["id"], now))
                continue
            out["checked"] += 1
            outcome = self.rt.pulse.update(account, lambda st, i=item, r=result: self._decided(st, i["id"], r, now))
            out[outcome] += 1
        return out

    @staticmethod
    def _failed(state, item_id, now):
        item = state["memory"]["items"].get(str(item_id))
        if item is not None and item["state"] == "open":
            pulse_followup.failed(item, now)

    @staticmethod
    def _decided(state, item_id, result, now):
        item = state["memory"]["items"].get(str(item_id))
        if item is None or item["state"] != "open":
            return "closed"
        news = pulse_followup.apply(item, result, now)
        if news:
            ref = f"m{item['id']}"
            fp = pulse.fingerprint("followup", ref, item["checks"], news)
            state["items"][fp] = {"fp": fp, "kind": "followup", "ref": ref, "status": "followup", "title": news,
                                  "source": "memory", "memory_kind": item["kind"], "at": now, "state": "candidate",
                                  "first_seen": now, "exposures": 0, "mentions": 0, "interrupted": 0, "log": [],
                                  "followup": None, "feedback": None, "acknowledged": None,
                                  "evidence": item["evidence"], "why": result["why"]}
            for old in state["items"].values():  # an older unheard follow-up on the same item is superseded
                if old is not state["items"][fp] and old.get("ref") == ref and old["state"] in pulse.UNREAD:
                    old["state"], old["superseded_by"] = "superseded", fp
            item["card"] = fp
            return "told"
        if item["state"] == "gave_up":
            return "gave_up"
        return "closed" if item["state"] != "open" else "waiting"

    # --- selection -----------------------------------------------------------------------------------
    def select(self, account, timeout=None):
        """Make sure the account's selection is the model's for the cards eligible now; returns how it stands."""
        now = self.rt.clock()
        state = self.rt.pulse.snapshot(account)
        pool = [c for c in state["items"].values() if pulse.eligible(c, now)]
        if not pool:
            return "empty"
        sig = pulse_select.signature(pool)
        current = state.get("selection") or {}
        if current.get("sig") == sig and current.get("by") == "model":
            return "model"
        if not self.model.available():
            if current.get("by") != "fallback" or current.get("sig") != sig:  # call setup writes nothing new
                self._record_fallback(account, sig, "no model route", now)
            return "fallback"
        pool.sort(key=lambda c: (-pulse.importance(c, state["memory"], now), c["at"], c["ref"]))
        recent = sorted(({"title": c["title"], "outcome": c["state"] if not c.get("feedback")
                          else f"{c['state']}, user said {c['feedback']['kind']}",
                          "at": c["log"][-1]["at"]} for c in state["items"].values() if c.get("log")),
                        key=lambda r: r["at"])
        titles = {}
        for card in state["items"].values():
            kind = (card.get("feedback") or {}).get("kind")
            if kind in ("useful", "not_useful", "dismiss"):
                titles.setdefault("useful" if kind == "useful" else "not_useful", []).append(card["title"])
        limit = self.rt.config.budgets.activity_items or 1
        try:
            decisions, picked = pulse_select.ask(self.model, pool, pulse_memory.remembered(state["memory"]), recent,
                                                 titles, now, limit, timeout=timeout)
        except ModelUnavailable as exc:
            self._record_fallback(account, sig, str(exc), now)
            return "fallback"
        over = sorted((fp for fp, d in decisions.items() if d["decision"] == pulse_select.OVER_LIMIT),
                      key=lambda fp: -decisions[fp]["value"])
        order = picked + over

        def save(st):
            st["selection"] = {"by": "model", "sig": sig, "at": now, "order": order, "decisions": decisions,
                               "model": self.model.label()}
            for fp, decision in decisions.items():
                card = st["items"].get(fp)
                if card is not None:
                    card["pick"] = {**decision, "at": now}
        self.rt.pulse.update(account, save)
        return "model"

    def _record_fallback(self, account, sig, reason, now):
        def save(st):
            st["selection"] = {"by": "fallback", "sig": sig, "at": now, "reason": reason}
        self.rt.pulse.update(account, save)


__all__ = ["PulseEngine"]
