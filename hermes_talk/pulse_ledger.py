"""The persistent Pulse record of one account: cards, their per-call evidence, feedback and memory.

One meta row per verified account scope (profile, bot, Telegram user), written through
SessionDB.get_meta/set_meta, so it survives reopen, restart and api/new and is never shared between
accounts. Calls are keyed by voice session id; a card is only ever moved by evidence from a call
that carried it. Feedback is keyed by its delegation id, so a resubmitted delegation changes nothing.
"""
import json
import threading

from . import pulse, pulse_policy, pulse_select

MAX_ITEMS = 500
MAX_SEEN = 2000
MAX_CALLS = 20
MAX_FEEDBACK = 200
MAX_TOLD = 50
LOG = 5
TOPIC_STEP = 0.05


class PulseLedger:
    def __init__(self, history, clock):
        self.history = history
        self.clock = clock
        self.lock = threading.Lock()

    @staticmethod
    def _key(account):
        return f"hermes_talk:pulse:{account.key}"

    def _load(self, account):
        raw = self.history.get_meta(self._key(account))
        try:
            state = json.loads(raw) if raw else None
        except ValueError:
            state = None
        if not isinstance(state, dict) or state.get("account") != account.key:
            state = {"account": account.key, "v": 1, "items": {}, "seen": {}, "calls": {}, "feedback": {},
                     "memory": {"sources": {}, "topics": {}, "told": []}, "scan": None}
        memory = state["memory"]
        for name, empty in (("items", {}), ("read", {}), ("talk", []), ("next_id", 1)):
            memory.setdefault(name, empty)
        state.setdefault("selection", None)
        return state

    def _save(self, account, state):
        self.history.set_meta(self._key(account), json.dumps(state, sort_keys=True))

    def scan_from(self, account, legacy_cursor):
        """The (last_active, id) key the next scan starts at; a ledger's first scan continues a legacy read cursor."""
        with self.lock:
            state = self._load(account)
        scan = state.get("scan") if state.get("scan") is not None else legacy_cursor
        return tuple(scan) if scan is not None else None

    def known(self, account):
        """{session id: (last_active, last_role, last_kind)} of sources already read, so unchanged ones are not reread."""
        with self.lock:
            return {ref: tuple(v) for ref, v in self._load(account)["seen"].items()}

    def refresh(self, account, rows, exhaustive, now):
        """Upsert one card revision per scanned row (row dicts carry status, title, source, id, last_active)."""
        with self.lock:
            state = self._load(account)
            items, memory = state["items"], state["memory"]
            by_ref = {}
            for item in items.values():
                by_ref.setdefault(item["ref"], []).append(item)
            for row in sorted(rows, key=lambda r: (r["last_active"], r["id"])):
                state["seen"][row["id"]] = [row["last_active"], row.get("last_role"), row.get("last_kind")]
                fp = pulse.fingerprint("activity", row["id"], row["status"], row["title"])
                if fp in items:
                    items[fp]["at"] = max(items[fp]["at"], row["last_active"])
                    continue
                card = {"fp": fp, "kind": "activity", "ref": row["id"], "status": row["status"],
                        "title": row["title"], "source": row["source"], "at": row["last_active"],
                        "state": "candidate", "first_seen": now, "exposures": 0, "mentions": 0,
                        "interrupted": 0, "log": [], "followup": None, "feedback": None, "acknowledged": None}
                for old in by_ref.get(row["id"], ()):
                    if old["fp"] == fp:
                        continue
                    if old["state"] in pulse.UNREAD or old["state"] == "snoozed":
                        old["state"], old["superseded_by"] = "superseded", fp
                    if (old.get("followup") or {}).get("state") == "watch":
                        old["followup"]["state"] = "done"
                        card["follows"] = old["fp"]
                key = pulse.content_key(row["status"], row["title"])
                twin = next((o for o in items.values() if o["ref"] != row["id"] and o["state"] in pulse.UNREAD
                             and pulse.content_key(o["status"], o["title"]) == key
                             and abs(o["at"] - row["last_active"]) <= pulse.DUPLICATE_WINDOW_S), None)
                told = next((t for t in memory["told"] if t["key"] == key and t["ref"] != row["id"]
                             and abs(t["at"] - row["last_active"]) <= pulse.DUPLICATE_WINDOW_S), None)
                if twin is not None or told is not None:
                    card["state"], card["duplicate_of"] = "duplicate", (twin or told)["fp"]
                items[fp] = card
                by_ref.setdefault(row["id"], []).append(card)
            if exhaustive and rows:
                last = max((r["last_active"], r["id"]) for r in rows)
                scan = state.get("scan")
                state["scan"] = list(last) if scan is None or tuple(scan) < last else scan
            self._trim(state)
            self._save(account, state)

    @staticmethod
    def _trim(state):
        items = state["items"]
        if len(items) > MAX_ITEMS:
            closed = sorted((i["at"], fp) for fp, i in items.items() if i["state"] in pulse.CLOSED)
            for _, fp in closed[: len(items) - MAX_ITEMS]:
                del items[fp]
        seen = state["seen"]
        for ref, _ in sorted(seen.items(), key=lambda kv: kv[1][0])[: max(0, len(seen) - MAX_SEEN)]:
            del seen[ref]
        calls = state["calls"]
        for vs, _ in sorted(calls.items(), key=lambda kv: kv[1]["at"])[: max(0, len(calls) - MAX_CALLS)]:
            del calls[vs]
        feedback = state["feedback"]
        for fid, _ in sorted(feedback.items(), key=lambda kv: kv[1]["at"])[: max(0, len(feedback) - MAX_FEEDBACK)]:
            del feedback[fid]

    def select(self, account, limit, now, exclude=()):
        """(cards, waiting): copies of the most important unread cards, none of the excluded fingerprints."""
        cards, waiting, _ = self.choose(account, limit, now, exclude)
        return cards, waiting

    def choose(self, account, limit, now, exclude=(), settings=None):
        """(cards, waiting, by): the model's selection when it covers exactly the eligible cards now, else the
        deterministic ranking (by "fallback"). A model pick carries its decision (why, value, line). With
        ``settings`` the delivery policy (pulse_policy) orders them loudest first and holds or turns off the
        rest; a held card comes with the first call that is the brief, which is stamped once per local day when
        a brief card is acknowledged (_acknowledge), never here: being chosen or supplied is not being heard."""
        with self.lock:
            state = self._load(account)
            pool = [dict(i) for i in state["items"].values() if pulse.eligible(i, now)]
            selection = state.get("selection")
            if selection and selection.get("by") == "model" and selection["sig"] == pulse_select.signature(pool):
                decisions = selection["decisions"]
                by_fp = {c["fp"]: c for c in pool}
                ordered = [dict(by_fp[fp], pick=decisions.get(fp)) for fp in selection["order"]
                           if fp in by_fp and fp not in exclude]
                withheld = sum(1 for c in pool if (decisions.get(c["fp"]) or {}).get("decision")
                               in (pulse_select.SKIPPED, pulse_select.NOT_PICKED))
                rest, by = len(pool) - withheld - len(set(exclude) & set(by_fp)), "model"
            else:
                ordered, rest = pulse.select([dict(c, pick=None) for c in pool if c["fp"] not in exclude],
                                             state["memory"], now, len(pool))
                by = "fallback"
            if settings is not None:
                ordered = self._deliver(account, state, ordered, settings, now)
            chosen = ordered[:limit]
            return chosen, (max(len(chosen), rest) if by == "model" else rest), by

    def _deliver(self, account, state, ordered, settings, now):
        brief = state.get("brief") or {}
        day = pulse_policy.brief_day(settings, now)
        owed = brief.get("cards") or () if brief.get("day") == day else ()
        delivered, held, off, is_brief = pulse_policy.arrange(ordered, state["memory"], settings, now,
                                                              brief.get("day"), owed)
        changed = False
        for card in delivered + held + off:  # written only when a card's level changes, never on every call
            item = state["items"].get(card["fp"])
            level = card["delivery"]["level"]
            if item is not None and (item.get("delivery") or {}).get("level") != level:
                item["delivery"] = {**card["delivery"], "at": now}
                changed = True
        if is_brief:
            # The brief this day would be, before any caller limit: offered, not consumed. It becomes the day's
            # brief only when one of its cards is acknowledged (_acknowledge); clipped or unheard cards stay owed.
            offer = {"day": day, "cards": [c["fp"] for c in delivered if c["delivery"]["level"] == "brief"]}
            if state.get("brief_offer") != offer:
                state["brief_offer"] = offer
                changed = True
        if changed:
            self._trim(state)
            self._save(account, state)
        return delivered

    def update(self, account, change):
        """Run change(state) under the account's lock and save; returns its result. Engine steps use this."""
        with self.lock:
            state = self._load(account)
            result = change(state)
            self._trim(state)
            self._save(account, state)
            return result

    def snapshot(self, account):
        with self.lock:
            return json.loads(json.dumps(self._load(account)))

    def exposed(self, account, voice_session_id, fps, via, now):
        """Record that the voice model of this call received these cards whole; returns their call handles."""
        with self.lock:
            state = self._load(account)
            call = state["calls"].setdefault(voice_session_id, {"at": now, "handles": {}, "pending": {},
                                                                "user_seq": -1})
            handles = {}
            for fp in fps:
                card = state["items"].get(fp)
                if card is None:
                    continue
                number = next((int(n) for n, f in call["handles"].items() if f == fp), None)
                if number is None:
                    number = len(call["handles"]) + 1
                    call["handles"][str(number)] = fp
                handles[fp] = f"P{number}"
                if card["state"] == "candidate" or (card["state"] == "snoozed" and pulse.eligible(card, now)):
                    card["state"] = "exposed"
                    if card.get("followup"):
                        card["followup"]["state"] = "done"
                card["exposures"] += 1
                card["log"] = (card["log"] + [{"step": "exposed", "call": voice_session_id, "via": via, "at": now}])[-LOG:]
            self._trim(state)
            self._save(account, state)
            return handles

    def handles(self, account, voice_session_id):
        """{fp: "P<n>"} already assigned in this call, so a context update keeps numbering after the instructions."""
        with self.lock:
            call = self._load(account)["calls"].get(voice_session_id) or {"handles": {}}
            return {fp: f"P{n}" for n, fp in call["handles"].items()}, len(call["handles"])

    def observe(self, account, voice_session_id, turns, now):
        """Move this call's cards on newly stored settled turns [(event id, sequence, role, text, interrupted)]."""
        with self.lock:
            state = self._load(account)
            call = state["calls"].get(voice_session_id)
            if call is None:
                return
            before = json.dumps(call, sort_keys=True)
            items = state["items"]
            carried = [items[fp] for fp in call["handles"].values() if fp in items]
            changed = False
            for event_id, sequence, role, text, interrupted in sorted(turns, key=lambda t: t[1]):
                if role == "user":
                    call["user_seq"] = max(call["user_seq"], sequence)
                    continue
                for card in carried:
                    others = [o for o in carried if o is not card]
                    if card["state"] not in pulse.UNREAD or not pulse.mentioned(text, card, others):
                        continue
                    changed = True
                    if interrupted:
                        card["interrupted"] += 1
                        card["log"] = (card["log"] + [{"step": "interrupted", "call": voice_session_id,
                                                       "event": event_id, "at": now}])[-LOG:]
                        continue
                    card["state"] = "mentioned"
                    card["mentions"] += 1
                    call["pending"][card["fp"]] = sequence
                    card["log"] = (card["log"] + [{"step": "mentioned", "call": voice_session_id,
                                                   "event": event_id, "at": now}])[-LOG:]
            for fp, sequence in list(call["pending"].items()):
                card = items.get(fp)
                if card is None or card["state"] != "mentioned":
                    del call["pending"][fp]
                elif call["user_seq"] > sequence:
                    self._acknowledge(state, card, voice_session_id, "reply", now)
                    del call["pending"][fp]
                    changed = True
            if changed or json.dumps(call, sort_keys=True) != before:
                self._save(account, state)

    def _acknowledge(self, state, card, voice_session_id, by, now):
        brief, offer = state.get("brief") or {}, state.get("brief_offer") or {}
        if card["fp"] in offer.get("cards", ()) and brief.get("day") != offer["day"]:
            state["brief"] = brief = {"day": offer["day"], "at": now, "cards": list(offer["cards"])}
        if card["fp"] in brief.get("cards", ()) and not card.get("briefed"):
            card["briefed"] = now
        card["state"] = "acknowledged"
        card["acknowledged"] = {"call": voice_session_id, "by": by, "at": now}
        card["log"] = (card["log"] + [{"step": "acknowledged", "call": voice_session_id, "by": by, "at": now}])[-LOG:]
        if card["status"] == "active":
            card["followup"] = {"state": "watch", "due": None, "at": now}
        item = state["memory"]["items"].get(str(card["ref"])[1:]) if card.get("kind") == "followup" else None
        if item is not None and item.get("card") == card["fp"]:
            item["reminded"], item["card"] = now, None
            item["log"] = (item["log"] + [{"told": voice_session_id, "by": by, "at": now}])[-8:]
        told = state["memory"]["told"]
        told.append({"fp": card["fp"], "ref": card["ref"], "key": pulse.content_key(card["status"], card["title"]),
                     "at": card["at"], "told": now})
        del told[:-MAX_TOLD]

    def feedback(self, account, voice_session_id, feedback_id, number, kind, now, snooze_s, reason=None,
                 settings=None):
        """(outcome, card copy) for feedback on card P<number> of this call; a repeated feedback id returns its first outcome."""
        with self.lock:
            state = self._load(account)
            key = f"{voice_session_id}:{feedback_id}"
            if key in state["feedback"]:
                prior = state["feedback"][key]
                return prior["outcome"], dict(state["items"].get(prior["fp"]) or {})
            call = state["calls"].get(voice_session_id) or {"handles": {}}
            fp = call["handles"].get(str(number))
            card = state["items"].get(fp) if fp else None
            if card is None:
                return None, None
            memory = state["memory"]
            counts = memory["sources"].setdefault(card["source"], {})
            if kind in ("useful", "not_useful", "dismiss"):
                counts[kind] = counts.get(kind, 0) + 1
                step = TOPIC_STEP if kind == "useful" else -TOPIC_STEP
                for word in set(pulse.tokens(card["title"])):
                    weight = memory["topics"].get(word, 0.0) + step
                    memory["topics"][word] = max(-1.0, min(1.0, round(weight, 4)))
                if len(memory["topics"]) > 400:
                    for word in sorted(memory["topics"], key=lambda w: abs(memory["topics"][w]))[:100]:
                        del memory["topics"][word]
            card["feedback"] = {"kind": kind, "id": feedback_id, "call": voice_session_id, "at": now}
            pulse_policy.signal(memory, card, kind, now)  # what pulse_prefs learns from
            if reason in pulse_policy.REASONS:
                card["feedback"]["reason"] = reason
                card["feedback"]["lesson"] = pulse_policy.reason(memory, card, reason,
                                                                 settings or pulse_policy.Settings(), now)
            if kind in ("useful", "more"):
                if card["state"] != "acknowledged":
                    self._acknowledge(state, card, voice_session_id, "feedback", now)
            elif kind in ("not_useful", "dismiss"):
                card["state"] = "dismissed"
                item = memory["items"].get(str(card["ref"])[1:]) if card.get("kind") == "followup" else None
                if item is not None and item.get("card") == fp and kind == "dismiss":
                    item["state"], item["card"] = "cancelled", None
                    item["outcome"] = {"by": "feedback", "decision": "dismiss", "call": voice_session_id, "at": now}
            else:
                card["state"] = "snoozed"
                card["followup"] = {"state": "scheduled", "due": now + snooze_s, "at": now}
            state["feedback"][key] = {"fp": fp, "outcome": kind, "at": now}
            self._trim(state)
            self._save(account, state)
            return kind, dict(card)

    def talk_sessions(self, account):
        with self.lock:
            return list(self._load(account)["memory"]["talk"])

    def remember_talk(self, account, conversation_id):
        """Note the account's Talk conversation as a memory source (newest last)."""
        with self.lock:
            state = self._load(account)
            talk = state["memory"]["talk"]
            if talk[-1:] == [conversation_id]:
                return
            state["memory"]["talk"] = ([c for c in talk if c != conversation_id] + [conversation_id])[-8:]
            self._save(account, state)

    def cards(self, account):
        with self.lock:
            return [dict(i) for i in self._load(account)["items"].values()]


__all__ = ["PulseLedger"]
