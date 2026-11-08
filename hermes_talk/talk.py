"""Hermes Telegram Talk runtime: the authenticated operations behind the /tg-voice/ API.

Every method takes a Telegram user id that server.py already verified from
signed initData and the allowlist. Profile and bot come from configuration
and the bot token, never from the request.
"""
import json
import logging
import random
import re
import time
from dataclasses import asdict

from . import activity, pulse, pulse_policy
from .agent import Readiness
from .gateway_rpc import task_agent
from .config import PRIVATE_NAMES
from .context import UPDATE_HEADER, chat_section, compose, update
from .jobs import DELEGATION_ID, OPEN, SUCCEEDED, UNDELIVERED, JobBoard
from .pulse_engine import SELECT_ON_CALL_S, PulseEngine
from .pulse_ledger import PulseLedger
from .pulse_model import PulseModel
from .receipts import DIRECTIVE, SINCE_KEY, Destination, ReportDesk, TelegramSender
from .store import SOURCE, TalkError, TalkStore, VoiceScope, clip
from .thread_inbox import ThreadInbox, history_text, item_line, receipt

logger = logging.getLogger("tg-voice")

SECRETS = [re.compile(p) for p in (
    r"\bsk-[A-Za-z0-9_-]{16,}",
    r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"(?i)\b(api[_-]?key|token|secret|password|bearer)\b\s*[:=]?\s*[A-Za-z0-9_\-./+]{12,}",
)]
CANCEL = re.compile(r"(?i)^\s*(?:please\s+)?(?:cancel|stop)\s+(?:hermes\s+)?task\s*#?\s*(\d{1,4})\b")
RESULT = re.compile(r"(?i)^\s*(?:the\s+)?(?:full\s+)?result\s+of\s+(?:hermes\s+)?task\s*#?\s*(\d{1,4})\s*[.!?]?\s*$")

SYNOPSIS_CHARS = 160
CHAT_CHARS = 8000  # chat input carried by one context update; the rest follows in the next one
TASK_PROMPT = (
    "[Hermes Telegram Talk voice task {number}. It was delegated from a Telegram voice call and nobody watches this run "
    "live. Use your tools as needed. Nobody can approve a flagged action during this run: if one is blocked, say "
    "so instead of working around it. End with a short plain-language result that can be read aloud, in the "
    "language of the conversation.]"
)
NOT_READY = Readiness(False, "Tasks are not available for this account.")
SNOOZE_S = 3 * 3600
PREPARING = ("- Activity since the last visit is still being read and may arrive later in this call. Until it does, "
             "say so if asked; do not guess.")
FEEDBACK_REPLIES = {
    "useful": "Noted that update {handle} was useful; Pulse will favor similar ones.",
    "not_useful": "Noted that update {handle} was not useful; Pulse will offer fewer like it.",
    "dismiss": "Update {handle} is dismissed and will not be offered again.",
    "later": "Update {handle} will be offered again in about three hours.",
}


def redact(text):
    for pattern in SECRETS:
        text = pattern.sub("[redacted]", text)
    return text


def native_soul():
    from agent.prompt_builder import load_soul_md
    return load_soul_md() or ""


def native_skill(name):
    from tools.skills_tool import skill_view
    result = json.loads(skill_view(name, preprocess=False))
    return result.get("content") or "" if result.get("success") else ""


class TalkRuntime:
    # Sessions read per visit for the briefing; past this the briefing says it is not exhaustive.
    activity_scan_max = 1000

    def __init__(self, config, bot_token, history, soul=native_soul, skill=native_skill, clock=time.time,
                 agent=None, home=None, sender=None, model=None):
        self.config = config
        self.bot_id = bot_token.split(":", 1)[0]
        self.history = history
        self.store = TalkStore(history, clock, config.voice_session_ttl_s, config.budgets.summary_chars)
        self.pulse = PulseLedger(history, clock)
        self.engine = PulseEngine(self, model if model is not None else PulseModel())
        self.soul = soul
        self.skill = skill
        self.clock = clock
        self._last_greeting = {}
        # Chat input from the exact Telegram thread a call continues (see thread_inbox.py); needs the profile home.
        self.inbox = ThreadInbox(home, clock) if home is not None else None
        self._threads = {}
        self.board = self.desk = None
        if config.tasks.enabled:
            self.agent = agent or task_agent(config.tasks, home)
            r = config.receipts
            if not r.enabled:
                history.set_meta(SINCE_KEY, "")  # turning reports back on later reports nothing from this gap
            if r.enabled:
                self.desk = ReportDesk(history, sender or TelegramSender(bot_token, r.send_timeout_s), clock,
                                       home or self.agent.home, r.max_chars, r.attempts, r.backoff_s, redact)
            self.board = JobBoard(history, self.agent, config.tasks, clock, self._task_prompt,
                                  lambda job, sid: self.store.bind_agent_session(job.conversation_id, sid), redact,
                                  self.desk)
            self.board.reconcile()

    def account(self, user_id):
        return VoiceScope(self.config.profile_id, self.bot_id, user_id)

    def readiness(self, user_id):
        if self.board is None or user_id not in self.config.tasks.user_ids:
            return NOT_READY if self.board is not None else Readiness(False, "Tasks are off in the Talk config.")
        return self.agent.readiness()

    def _public(self, user_id, conversation_id, avatar_url):
        c = self.config
        public = {"conversation_id": conversation_id,
                  "profile": {"id": c.profile_id, "display_name": c.display_name, "avatar_url": avatar_url},
                  "appearance": {"listen_color": c.listen_color, "speak_color": c.speak_color},
                  "greeting_enabled": c.greeting_enabled,
                  "limits": {"event_batch_max": c.budgets.event_batch_max}}
        if self.board is not None:
            public["tasks"] = {"available": self.readiness(user_id).ready}
        return public

    def bootstrap(self, user_id, avatar_url):
        conversation_id, _ = self.store.current(self.account(user_id))
        return self._public(user_id, conversation_id, avatar_url)

    def new(self, user_id, conversation_id, avatar_url):
        conversation_id, _ = self.store.new(self.account(user_id), conversation_id)
        return self._public(user_id, conversation_id, avatar_url)

    def _scan(self, account, user_id, conversation_id, state):
        """Read the profile's changed sessions into the account's Pulse cards; returns a note for the briefing."""
        c = self.config
        if not c.activity_enabled or c.budgets.activity_items == 0:
            return ""
        now = self.clock()
        legacy = state.get("activity_cursor")
        since = self.pulse.scan_from(account, list(activity.cursor_key(legacy)) if legacy is not None else None)
        if since is None:
            since = (now - c.activity_window_s, "")
        try:
            rows, exhaustive = self.history.activity_rows(SOURCE, since[0], self.activity_scan_max)
            mine = activity.unread(rows, since=since, user_id=user_id, owner_user_id=c.activity_owner_user_id,
                                   sources=c.activity_sources, exclude_ids={conversation_id})
            known = self.pulse.known(account)
            scanned = []
            for row in mine:
                prior = known.get(row["id"])
                if prior is not None and prior[0] == row["last_active"]:
                    last_role, last_kind = prior[1], prior[2]
                else:
                    last_role, last_kind = self.history.last_turn(row["id"])
                full = {**row, "last_role": last_role, "last_kind": last_kind}
                scanned.append({**full, "status": activity.status(full, now), "title": activity.title(full)})
            self.pulse.refresh(account, scanned, exhaustive, now)
        except Exception as exc:  # a briefing is never worth the call
            note = f"- Activity could not be read ({type(exc).__name__}). Say so if asked; do not guess."
        else:
            note = "" if exhaustive else activity.NOT_EXHAUSTIVE
        self.pulse.update(account, lambda st: st.__setitem__("prepared", {"at": now, "note": note}))
        return note

    def _prepared_note(self, account):
        """The briefing note of the account's last background scan, or that its activity is still being read."""
        prepared = self.pulse.snapshot(account).get("prepared")
        return PREPARING if prepared is None else prepared["note"]

    def pulse_refresh(self, user_id):
        """Scan the profile's changed sessions into the account's cards outside a call (the engine's tick)."""
        account = self.account(user_id)
        talk = self.pulse.talk_sessions(account)
        if not talk:
            conversation_id, state = self.store.current(account)
            self._scan(account, user_id, conversation_id, state)
            return
        try:
            state = self.store.owned(account, talk[-1], bound=True)
        except TalkError:
            return  # that conversation is no longer the account's current one; the next call scans
        self._scan(account, user_id, talk[-1], state)

    def _cards(self, account, limit, exclude=(), first_handle=1, select=True):
        """(cards, waiting, lines): the selected Pulse cards and their briefing lines, numbered from first_handle."""
        now = self.clock()
        if select:
            self.engine.select(account, timeout=SELECT_ON_CALL_S)
        cards, waiting, by = self.pulse.choose(account, limit, now, exclude, pulse_policy.settings_from(self.config))
        lines = [redact(pulse.line(card, f"P{first_handle + i}", activity.when(card["at"])))
                 for i, card in enumerate(cards)]
        logger.info("pulse selection by=%s cards=%d waiting=%d", by, len(cards), waiting)
        return cards, waiting, lines

    def _job_items(self, account, conversation_id, exclude_voice_session=None):
        """(id, line, synopsis) task outcomes not yet handed to the voice model, oldest first, then open tasks."""
        if self.board is None:
            return []
        jobs = self.board.of_conversation(account.key, conversation_id)
        done = [(j.id, "- " + redact(j.spoken()), self._synopsis(j)) for j in jobs
                if j.terminal and j.receipt == UNDELIVERED and j.voice_session_id != exclude_voice_session]
        running = [(f"open:{j.id}", f"- Hermes task {j.number} ({clip(redact(j.request), 80)}) is {j.state}.", "")
                   for j in jobs if j.state in OPEN]
        return done + running

    @staticmethod
    def _synopsis(job):
        body = job.result if job.state == SUCCEEDED else job.error
        return (f"- Hermes task {job.number} ({clip(redact(job.request), 60)}) {job.spoken_state()}. "
                f"Its result is long ({len(body)} characters); it begins: {clip(redact(body), SYNOPSIS_CHARS)} "
                f"To hear all of it, delegate exactly \"result of task {job.number}\".")

    def _destinations(self, user_id):
        return [d for d in self.config.receipts.destinations if user_id in d.user_ids] if self.desk else []

    def _reports_note(self, user_id):
        if self.desk is None:
            return ""
        names = ", ".join(d.name for d in self._destinations(user_id))
        note = (" When a task finishes, Hermes also sends a short report to the user's Telegram chat with this bot, "
                "even after the call ends.")
        if names:
            note += (f" Only if the user explicitly asks for the report to go to one of: {names}, end the delegated "
                     "task with a line \"REPORT TO: <name>\"; it then goes there instead.")
        return note

    def _report_to(self, user_id, request):
        """(request without a directive, bound destination): the user's own chat unless they named a configured one."""
        match = DIRECTIVE.search(request)
        if match is None or self.desk is None:
            return request, asdict(Destination.private(user_id, self.bot_id)) if self.desk else None
        name = " ".join(match.group(1).split()).lower()
        stripped = (request[:match.start()] + request[match.end():]).strip()
        if name in PRIVATE_NAMES:
            return stripped, asdict(Destination.private(user_id, self.bot_id))
        for d in self._destinations(user_id):
            if d.name.lower() == name:
                return stripped, asdict(Destination("named", d.chat_id, d.thread_id, d.name, self.bot_id))
        names = ", ".join(d.name for d in self._destinations(user_id)) or "none"
        raise TalkError(403, f"That report destination is not available for this account (available: {names}). "
                             "Nothing was started.")

    def prepare(self, user_id, conversation_id):
        """Instructions for the next call on the account's current conversation."""
        account = self.account(user_id)
        if conversation_id is None:
            conversation_id, state = self.store.current(account)
        else:
            state = self.store.owned(account, conversation_id, bound=True)
        c = self.config
        soul = redact(self.soul() or "")
        skills = [(name, redact(self.skill(name) or "")) for name in c.skills]
        turns = [(role, redact(text)) for role, text in self.history.recent(conversation_id, c.budgets.recent_turns)]
        self.pulse.remember_talk(account, conversation_id)
        limit = c.budgets.activity_items if c.activity_enabled else 0
        # Call setup reads only the account's prepared Pulse state; scanning and model selection catch up in the
        # background and reach this call through its context refreshes.
        cards, total, lines = self._cards(account, limit, select=False) if limit else ([], 0, [])
        note = self._prepared_note(account) if limit else ""
        self.engine.prepare_soon(user_id)
        job_items = self._job_items(account, conversation_id)
        instructions, report = compose(c, soul, [s for s in skills if s[1]], redact(state.get("summary") or ""),
                                       turns, lines, total, note, self.readiness(user_id).ready,
                                       job_items, self._reports_note(user_id), self._greeting_line(account))
        # Only cards the final text carries whole are exposed; none counts as heard until the call shows it.
        carried = [card["fp"] for card in cards[: report["activity_items"]]]
        report["job_ids"] = [i for i in report["job_ids"] if not i.startswith("open:")]
        report["job_numbers"] = self._job_numbers(account, conversation_id, report["job_ids"])
        logger.info("context frame=instructions sources=%s", json.dumps(
            {key: report[key] for key in ("frame", "soul", "skills", "summary", "recent", "jobs", "activity",
                                          "greeting", "activity_items")}, sort_keys=True))
        return conversation_id, instructions, carried, report

    def _greeting_line(self, account):
        """One of the profile's configured ready greetings, never the one this account heard last."""
        phrases = self.config.greeting_phrases
        if not phrases:
            return None
        last = self._last_greeting.get(account.key)
        choices = [p for p in phrases if p != last] or list(phrases)
        line = random.choice(choices)
        self._last_greeting[account.key] = line
        return line

    def _voice_key(self, user_id):
        return f"hermes_talk:voice:{self.account(user_id).key}"

    def reply_voice(self, user_id):
        """The account's chosen reply voice if the config still offers it, else the profile default."""
        chosen = self.history.get_meta(self._voice_key(user_id)) if user_id is not None else None
        return chosen if chosen in dict(self.config.voice_choices) else self.config.voice

    def _voice_public(self, user_id):
        return {"current": self.reply_voice(user_id), "default": self.config.voice,
                "choices": [{"id": i, "label": label} for i, label in self.config.voice_choices],
                "applies": "next_connection"}

    def set_voice(self, user_id, voice=None):
        """Read (voice None) or persist the reply voice for (profile, bot, user). It applies from the next connection:
        the provider sets the voice when a session is created, so a live call keeps its voice until it reconnects."""
        if voice is None:
            return {"voice": self._voice_public(user_id)}
        if not isinstance(voice, str) or voice not in dict(self.config.voice_choices):
            raise TalkError(400, "That reply voice is not offered for this profile.")
        self.history.set_meta(self._voice_key(user_id), voice)
        return {"voice": self._voice_public(user_id)}

    def session_config(self, instructions, user_id=None):
        return {"model": self.config.model, "instructions": instructions,
                "audio": {"output": {"voice": self.reply_voice(user_id)}}, "delegation": {"type": "client"}}

    def _job_numbers(self, account, conversation_id, job_ids):
        if self.board is None or not job_ids:
            return {}
        wanted = set(job_ids)
        return {j.id: j.number for j in self.board.of_conversation(account.key, conversation_id) if j.id in wanted}

    def started(self, user_id, conversation_id, cards, job_ids=(), job_numbers=None, thread=None):
        account = self.account(user_id)
        voice_session_id = self.store.start_voice_session(account, conversation_id, None, job_ids, job_numbers)
        if cards:
            self.pulse.exposed(account, voice_session_id, cards, "instructions", self.clock())
        if thread is not None and self.inbox is not None and thread.user_id == user_id and thread.bot_id == self.bot_id:
            orphans = self.inbox.bind(thread, conversation_id, voice_session_id, self.config.voice_session_ttl_s)
            with self.inbox.locked(thread.key):
                self._keep_chat(thread, None, orphans)  # an earlier conversation's unheard input, kept in its own
            self._threads[voice_session_id] = thread
        return voice_session_id

    def _chat_items(self, voice_session_id, conversation_id):
        """Pending chat input of this call's exact thread and conversation: [(name, item)].

        Input an earlier call of the same thread and conversation took but never delivered (a minimized page that
        reconnected without api/end) belongs to this call too; another thread's or conversation's never does."""
        thread = self._threads.get(voice_session_id)
        if thread is None or self.inbox is None:
            return thread, []
        items = [(n, i) for n, i in self.inbox.pending(thread.key) if i.get("conversation_id") == conversation_id]
        return thread, items

    def _keep_chat(self, thread, conversation_id, items):
        """Append chat input to its conversation once (receipt-deduplicated), then settle it.

        Callers hold the thread lock (`ThreadInbox.locked`), so an ack and an end of the same thread cannot both
        pass the receipt check. conversation_id None keeps each item in the conversation it was taken for."""
        for name, item in items:
            target = conversation_id or item.get("conversation_id")
            if not isinstance(target, str) or not target:
                continue
            mark = receipt(item)
            if not self.history.has_receipt(target, mark):
                self.history.append(target, [{"role": "user", "content": redact(history_text(item)),
                                              "platform_message_id": mark, "timestamp": self.clock()}])
            self.inbox.settle(thread.key, name)

    def events(self, user_id, conversation_id, voice_session_id, events):
        b = self.config.budgets
        account = self.account(user_id)
        accepted, turns, record = self.store.ingest(account, conversation_id, voice_session_id, events,
                                                    b.event_batch_max, b.event_text_chars)
        if turns:
            self.pulse.observe(account, voice_session_id, turns, self.clock())
            numbers = {int(n): job_id for job_id, n in (record.get("job_numbers") or {}).items()}
            said = set()
            for _, _, role, text, interrupted in turns:
                if role == "assistant" and not interrupted:
                    said |= pulse.tasks_mentioned(text, set(numbers))
            if said and self.board is not None:
                self.board.mark_delivered([numbers[n] for n in sorted(said)], "instructions")
        return {"accepted_ids": accepted, "conversation_id": conversation_id}

    def pulse_feedback(self, user_id, conversation_id, voice_session_id, delegation_item_id, request):
        """The user's reaction to one card of the live call, delegated as "pulse P<n> <kind>"; idempotent per delegation."""
        account = self.account(user_id)
        if not isinstance(delegation_item_id, str) or not DELEGATION_ID.fullmatch(delegation_item_id):
            raise TalkError(400, "Malformed delegation id.")
        parsed = pulse.parse_feedback(request) if isinstance(request, str) and len(request) <= 200 else None
        if parsed is None:
            raise TalkError(400, "Pulse feedback must be \"pulse P<number> useful|not useful|dismiss|later|more\", "
                                 "optionally with a reason: not relevant|already knew|too late|too often|wrong time.")
        self.store.voice_session(account, conversation_id, voice_session_id, live=True)
        number, kind = parsed
        outcome, card = self.pulse.feedback(account, voice_session_id, delegation_item_id, number, kind,
                                            self.clock(), SNOOZE_S, pulse.feedback_reason(request),
                                            pulse_policy.settings_from(self.config))
        if outcome is None:
            raise TalkError(404, f"Update P{number} is not part of this call.")
        handle = f"P{number}"
        if outcome == "more":
            reply = redact(f"Update {handle}: [{card['status']}] {card['title']} ({card['source']} session "
                           f"{card['ref']}, {activity.when(card['at'])}). That is everything Pulse holds about it.")
        else:
            reply = FEEDBACK_REPLIES[outcome].format(handle=handle)
        return {"card": {"handle": handle, "state": card["state"], "feedback": outcome}, "channel": "speakable",
                "reply": reply}

    def end(self, user_id, conversation_id, voice_session_id):
        ended = self.store.end(self.account(user_id), conversation_id, voice_session_id)
        thread = self._threads.get(voice_session_id)
        if thread is not None and self.inbox is not None:
            def flush(pending):  # not heard in the call, but kept for the next one (runs under the thread lock)
                self._keep_chat(thread, conversation_id,
                                [(n, i) for n, i in pending if i.get("conversation_id") == conversation_id])
            # A newer call of this thread that already holds the binding keeps it and still gets the input.
            self.inbox.unbind(thread, voice_session_id, flush)
            self._threads.pop(voice_session_id, None)
        self.engine.poke(user_id)  # the call went quiet: read it for memory and run due follow-ups
        return ended

    def _task_prompt(self, job):
        """(prompt, resume): the request once, after the bounded voice turns that preceded it."""
        b = self.config.budgets
        request = job.request.strip()
        turns = [(role, text) for role, text in self.history.recent(job.conversation_id, b.recent_turns)
                 if text.strip() != request]
        voice = "\n".join(f"{role}: {clip(redact(text), b.turn_chars)}" for role, text in turns)
        parts = [TASK_PROMPT.format(number=job.number)]
        if voice:
            parts.append("Voice conversation so far (oldest first):\n" + voice)
        parts.append(f"TASK: {request}")
        return "\n\n".join(parts), self.store.agent_session(job.conversation_id)

    def _tasks_for(self, user_id):
        readiness = self.readiness(user_id)
        if not readiness.ready:
            raise TalkError(503, readiness.reason or NOT_READY.reason)
        return self.account(user_id)

    def _board_for(self, user_id):
        """Cancel and acknowledge stay possible when readiness lapses; only ownership is required."""
        if self.board is None:
            raise TalkError(503, "Tasks are off in the Talk config.")
        return self.account(user_id)

    def _public_job(self, job):
        return job.public(self.desk.get(job.id) if self.desk else None)

    def task(self, user_id, conversation_id, voice_session_id, delegation_item_id, request):
        """Start a native task for a delegation of the live call, cancel one ("cancel task N") or repeat
        one's stored result ("result of task N")."""
        cancel = CANCEL.match(request) if isinstance(request, str) else None
        recall = RESULT.match(request) if isinstance(request, str) else None
        account = self._board_for(user_id)
        if not isinstance(delegation_item_id, str) or not DELEGATION_ID.fullmatch(delegation_item_id):
            raise TalkError(400, "Malformed delegation id.")
        limit = self.config.tasks.request_chars
        if not isinstance(request, str) or not request.strip() or len(request) > limit:
            raise TalkError(400, f"A task request must be 1..{limit} characters.")
        record = self.store.voice_session(account, conversation_id, voice_session_id, live=True)
        # A resubmitted delegation (its first response was lost) gets its job back even if readiness changed since.
        known = self.board.existing(account.key, conversation_id, record.get("generation", 0), delegation_item_id)
        if known is not None and not (cancel or recall):
            return {"job": self._public_job(known), "created": False, "channel": "commentary",
                    "reply": f"This was already sent to Hermes as task {known.number}; it is {known.state}."}
        if not (cancel or recall):
            self._tasks_for(user_id)
        if cancel:
            job = self.board.by_number(account.key, conversation_id, int(cancel.group(1)))
            job = self.board.cancel(account.key, conversation_id, job.id)
            return {"job": self._public_job(job), "created": False, "channel": "speakable",
                    "reply": f"Hermes task {job.number} " + ("is being cancelled." if job.state in OPEN else f"already {job.state}.")}
        if recall:
            job = self.board.by_number(account.key, conversation_id, int(recall.group(1)))
            if not job.terminal:
                reply = f"Hermes task {job.number} is still {job.state}; it has no result yet."
            else:
                reply = redact(job.spoken())
                if job.state == SUCCEEDED and len(job.result) >= self.config.tasks.result_chars - 1:
                    reply += " That is as much of the result as voice keeps."
            return {"job": self._public_job(job), "created": False, "channel": "speakable", "reply": reply}
        request, report_to = self._report_to(user_id, request)
        if not request:
            raise TalkError(400, "A task request must say what to do.")
        job, created = self.board.submit(account.key, conversation_id, voice_session_id, record.get("generation", 0),
                                         delegation_item_id, " ".join(request.split()), report_to)
        reply = (f"Hermes is working on this as background task {job.number}. Its result will follow on this "
                 "delegation; do not guess it." if created and job.state in OPEN else
                 f"This was already sent to Hermes as task {job.number}; it is {job.state}.")
        return {"job": self._public_job(job), "created": created, "channel": "commentary", "reply": reply}

    def tasks(self, user_id, conversation_id, voice_session_id):
        account = self.account(user_id)
        self.store.voice_session(account, conversation_id, voice_session_id)
        jobs = self.board.of_conversation(account.key, conversation_id) if self.board else []
        pending = any(j.terminal and j.receipt == UNDELIVERED and j.voice_session_id != voice_session_id for j in jobs)
        return {"jobs": [self._public_job(j) for j in jobs], "context_pending": pending,
                "tasks": {"available": self.readiness(user_id).ready}}

    def cancel(self, user_id, conversation_id, job_id):
        account = self._board_for(user_id)
        self.store.owned(account, conversation_id)
        return {"job": self._public_job(self.board.cancel(account.key, conversation_id, job_id))}

    def ack(self, user_id, conversation_id, voice_session_id, job_id):
        account = self._board_for(user_id)
        self.store.voice_session(account, conversation_id, voice_session_id)
        return {"job": self._public_job(self.board.ack(account.key, conversation_id, voice_session_id, job_id))}

    def context(self, user_id, conversation_id, voice_session_id):
        """An explicit mid-call refresh: task outcomes and Pulse cards the live call has not received yet."""
        account = self.account(user_id)
        record = self.store.voice_session(account, conversation_id, voice_session_id, live=True)
        state = self.store.owned(account, conversation_id, bound=True)
        note = self._scan(account, user_id, conversation_id, state)
        handled, count = self.pulse.handles(account, voice_session_id)
        limit = self.config.budgets.activity_items if self.config.activity_enabled else 0
        cards, total, lines = self._cards(account, limit, set(handled), count + 1) if limit else ([], 0, [])
        carried = set(record.get("job_ids") or ())
        items = [i for i in self._job_items(account, conversation_id, voice_session_id)
                 if not i[0].startswith("open:") and i[0] not in carried]
        revision = record.get("revision", 0) + 1
        text, job_ids, k = update(revision, items, lines, total, note, self.config.budgets.activity_chars
                                  + self.config.budgets.tasks_chars)
        _, chat = self._chat_items(voice_session_id, conversation_id)
        chat_text, n = chat_section([redact(item_line(f"C{i + 1}", item)) for i, (_, item) in enumerate(chat)],
                                    CHAT_CHARS)
        if chat_text:
            text = (text + "\n\n" + chat_text) if text else UPDATE_HEADER.format(revision=revision) + chat_text
        if not text:
            return {"revision": None, "text": "", "sources": {"jobs": 0, "activity": 0}}
        revision = self.store.save_snapshot(account, conversation_id, voice_session_id,
                                            [card["fp"] for card in cards[:k]], job_ids, [name for name, _ in chat[:n]])
        sources = {"jobs": len(job_ids), "activity": k}
        if n:
            sources["inputs"] = n  # additive: present only when the update carries chat input
        logger.info("context frame=session.context.append revision=%s sources=%s", revision, json.dumps(sources))
        return {"revision": revision, "text": text, "sources": sources}

    def context_ack(self, user_id, conversation_id, voice_session_id, revision):
        account = self.account(user_id)
        job_ids, cards, inputs = self.store.ack_snapshot(account, conversation_id, voice_session_id, revision)
        thread = self._threads.get(voice_session_id)
        if inputs and thread is not None and self.inbox is not None:
            with self.inbox.locked(thread.key):
                _, chat = self._chat_items(voice_session_id, conversation_id)
                self._keep_chat(thread, conversation_id, [(n, i) for n, i in chat if n in set(inputs)])
        if cards:
            self.pulse.exposed(account, voice_session_id, cards, f"context:{revision}", self.clock())
        if job_ids and self.board is not None:
            self.board.mark_delivered(job_ids, "context")
        return {"ok": True}


__all__ = ["TalkRuntime", "TalkError", "redact"]
