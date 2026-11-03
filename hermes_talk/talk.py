"""Hermes Talk runtime: the authenticated operations behind the /tg-voice/ API.

Every method takes a Telegram user id that server.py already verified from
signed initData and the allowlist. Profile and bot come from configuration
and the bot token, never from the request.
"""
import json
import logging
import re
import time

from . import activity
from .agent import NativeAgent, Readiness
from .context import compose, update
from .jobs import DELEGATION_ID, OPEN, UNDELIVERED, JobBoard
from .store import SOURCE, TalkError, TalkStore, VoiceScope, clip

logger = logging.getLogger("tg-voice")

SECRETS = [re.compile(p) for p in (
    r"\bsk-[A-Za-z0-9_-]{16,}",
    r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"(?i)\b(api[_-]?key|token|secret|password|bearer)\b\s*[:=]?\s*[A-Za-z0-9_\-./+]{12,}",
)]
CANCEL = re.compile(r"(?i)^\s*(?:please\s+)?(?:cancel|stop)\s+(?:hermes\s+)?task\s*#?\s*(\d{1,4})\b")
TASK_PROMPT = (
    "[Hermes Talk voice task {number}. It was delegated from a Telegram voice call and nobody watches this run "
    "live. Use your tools as needed. Nobody can approve a flagged action during this run: if one is blocked, say "
    "so instead of working around it. End with a short plain-language result that can be read aloud.]"
)
NOT_READY = Readiness(False, "Tasks are not available for this account.")


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
                 agent=None, home=None):
        self.config = config
        self.bot_id = bot_token.split(":", 1)[0]
        self.history = history
        self.store = TalkStore(history, clock, config.voice_session_ttl_s, config.budgets.summary_chars)
        self.soul = soul
        self.skill = skill
        self.clock = clock
        self.board = None
        if config.tasks.enabled:
            self.agent = agent or NativeAgent(config.tasks, home)
            self.board = JobBoard(history, self.agent, config.tasks, clock, self._task_prompt,
                                  lambda job, sid: self.store.bind_agent_session(job.conversation_id, sid), redact)
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

    def _briefing(self, user_id, conversation_id, state, since=None):
        """(candidate rows, unread total, note) for the next call; nothing is marked read here."""
        c = self.config
        if not c.activity_enabled or c.budgets.activity_items == 0:
            return [], 0, ""
        now = self.clock()
        cursor = state.get("activity_cursor")
        if since is None:
            since = activity.cursor_key(cursor) if cursor is not None else (now - c.activity_window_s, "")
        try:
            rows, exhaustive = self.history.activity_rows(SOURCE, since[0], self.activity_scan_max)
            pending = activity.unread(rows, since=since, user_id=user_id, owner_user_id=c.activity_owner_user_id,
                                      sources=c.activity_sources, exclude_ids={conversation_id})
            candidates = []
            for row in pending[: c.budgets.activity_items]:
                last_role, last_kind = self.history.last_turn(row["id"])
                candidates.append({**row, "last_role": last_role, "last_kind": last_kind})
        except Exception as exc:  # a briefing is never worth the call
            return [], 0, f"- Activity could not be read ({type(exc).__name__}). Say so if asked; do not guess."
        return candidates, len(pending), "" if exhaustive else activity.NOT_EXHAUSTIVE

    def _job_items(self, account, conversation_id, exclude_voice_session=None):
        """(id, line) task outcomes not yet handed to the voice model, then open tasks as status."""
        if self.board is None:
            return []
        jobs = self.board.of_conversation(account.key, conversation_id)
        done = [(j.id, "- " + redact(j.spoken())) for j in jobs
                if j.terminal and j.receipt == UNDELIVERED and j.voice_session_id != exclude_voice_session]
        running = [(f"open:{j.id}", f"- Hermes task {j.number} ({clip(redact(j.request), 80)}) is {j.state}.")
                   for j in jobs if j.state in OPEN]
        return done + running

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
        candidates, total, note = self._briefing(user_id, conversation_id, state)
        now = self.clock()
        lines = [redact(activity.line(row, now)) for row in candidates]
        instructions, report = compose(c, soul, [s for s in skills if s[1]], redact(state.get("summary") or ""),
                                       turns, lines, total, note, self.readiness(user_id).ready,
                                       self._job_items(account, conversation_id))
        # Only items the final text carries whole can be marked read once the assistant speaks.
        k = report["activity_items"]
        watermark = list(activity.key(candidates[k - 1])) if k else None
        report["job_ids"] = [i for i in report["job_ids"] if not i.startswith("open:")]
        logger.info("context frame=instructions sources=%s", json.dumps(
            {key: report[key] for key in ("frame", "soul", "skills", "summary", "recent", "jobs", "activity",
                                          "greeting", "activity_items")}, sort_keys=True))
        return conversation_id, instructions, watermark, report

    def session_config(self, instructions):
        return {"model": self.config.model, "instructions": instructions,
                "audio": {"output": {"voice": self.config.voice}}, "delegation": {"type": "client"}}

    def started(self, user_id, conversation_id, watermark, job_ids=()):
        return self.store.start_voice_session(self.account(user_id), conversation_id, watermark, job_ids)

    def events(self, user_id, conversation_id, voice_session_id, events):
        b = self.config.budgets
        accepted, spoke = self.store.ingest(self.account(user_id), conversation_id, voice_session_id, events,
                                            b.event_batch_max, b.event_text_chars)
        if spoke and self.board is not None:
            self.board.mark_delivered(spoke, "instructions")
        return {"accepted_ids": accepted, "conversation_id": conversation_id}

    def end(self, user_id, conversation_id, voice_session_id):
        return self.store.end(self.account(user_id), conversation_id, voice_session_id)

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

    def task(self, user_id, conversation_id, voice_session_id, delegation_item_id, request):
        """Start a native task for a delegation of the live call, or cancel one ("cancel task N")."""
        cancel = CANCEL.match(request) if isinstance(request, str) else None
        account = self._board_for(user_id) if cancel else self._tasks_for(user_id)
        if not isinstance(delegation_item_id, str) or not DELEGATION_ID.fullmatch(delegation_item_id):
            raise TalkError(400, "Malformed delegation id.")
        limit = self.config.tasks.request_chars
        if not isinstance(request, str) or not request.strip() or len(request) > limit:
            raise TalkError(400, f"A task request must be 1..{limit} characters.")
        record = self.store.voice_session(account, conversation_id, voice_session_id, live=True)
        if cancel:
            job = self.board.by_number(account.key, conversation_id, int(cancel.group(1)))
            job = self.board.cancel(account.key, conversation_id, job.id)
            return {"job": job.public(), "created": False, "channel": "speakable",
                    "reply": f"Hermes task {job.number} " + ("is being cancelled." if job.state in OPEN else f"already {job.state}.")}
        job, created = self.board.submit(account.key, conversation_id, voice_session_id, record.get("generation", 0),
                                         delegation_item_id, " ".join(request.split()))
        reply = (f"Hermes is working on this as background task {job.number}. Its result will follow on this "
                 "delegation; do not guess it." if created and job.state in OPEN else
                 f"This was already sent to Hermes as task {job.number}; it is {job.state}.")
        return {"job": job.public(), "created": created, "channel": "commentary", "reply": reply}

    def tasks(self, user_id, conversation_id, voice_session_id):
        account = self.account(user_id)
        self.store.voice_session(account, conversation_id, voice_session_id)
        jobs = self.board.of_conversation(account.key, conversation_id) if self.board else []
        pending = any(j.terminal and j.receipt == UNDELIVERED and j.voice_session_id != voice_session_id for j in jobs)
        return {"jobs": [j.public() for j in jobs], "context_pending": pending,
                "tasks": {"available": self.readiness(user_id).ready}}

    def cancel(self, user_id, conversation_id, job_id):
        account = self._board_for(user_id)
        self.store.owned(account, conversation_id)
        return {"job": self.board.cancel(account.key, conversation_id, job_id).public()}

    def ack(self, user_id, conversation_id, voice_session_id, job_id):
        account = self._board_for(user_id)
        self.store.voice_session(account, conversation_id, voice_session_id)
        return {"job": self.board.ack(account.key, conversation_id, voice_session_id, job_id).public()}

    def context(self, user_id, conversation_id, voice_session_id):
        """An explicit mid-call refresh: task outcomes and Pulse items the live call has not received yet."""
        account = self.account(user_id)
        record = self.store.voice_session(account, conversation_id, voice_session_id, live=True)
        state = self.store.owned(account, conversation_id, bound=True)
        keys = [activity.cursor_key(k) for k in (state.get("activity_cursor"), record.get("briefing_watermark"),
                                                 record.get("context_watermark")) if k is not None]
        since = max(keys) if keys else None
        candidates, total, note = self._briefing(user_id, conversation_id, state, since)
        now = self.clock()
        lines = [redact(activity.line(row, now)) for row in candidates]
        carried = set(record.get("job_ids") or ())
        items = [i for i in self._job_items(account, conversation_id, voice_session_id)
                 if not i[0].startswith("open:") and i[0] not in carried]
        revision = record.get("revision", 0) + 1
        text, job_ids, k = update(revision, items, lines, total, note, self.config.budgets.activity_chars
                                  + self.config.budgets.tasks_chars)
        watermark = list(activity.key(candidates[k - 1])) if k else None
        if not text:
            return {"revision": None, "text": "", "sources": {"jobs": 0, "activity": 0}}
        revision = self.store.save_snapshot(account, conversation_id, voice_session_id, watermark, job_ids)
        sources = {"jobs": len(job_ids), "activity": k}
        logger.info("context frame=session.context.append revision=%s sources=%s", revision, json.dumps(sources))
        return {"revision": revision, "text": text, "sources": sources}

    def context_ack(self, user_id, conversation_id, voice_session_id, revision):
        account = self.account(user_id)
        job_ids = self.store.ack_snapshot(account, conversation_id, voice_session_id, revision)
        if job_ids and self.board is not None:
            self.board.mark_delivered(job_ids, "context")
        return {"ok": True}


__all__ = ["TalkRuntime", "TalkError", "redact"]
