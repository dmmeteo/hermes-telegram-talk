"""Account-bound Talk conversations kept in Hermes' own session store.

A conversation is one native Hermes session titled "Telegram Voice". The
binding (verified Telegram user + configured profile + bot -> session id),
voice-session ledger, summary and legacy activity cursor live in the store's meta
table through SessionDB.get_meta/set_meta. Settled turns are appended as
ordinary user/assistant rows with a receipt, so a retried batch is
recognized instead of written twice. Raw audio, SDP and reasoning never
reach this module.
"""
import json
import re
import secrets
import threading
from dataclasses import dataclass

TITLE = "Telegram Voice"
SOURCE = "telegram-voice"
EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
VOICE_SESSION_ID = re.compile(r"vs_[A-Za-z0-9_-]{16,64}")
CONVERSATION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
ROLES = ("user", "assistant")


class TalkError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class VoiceScope:
    """The verified identity a conversation and its tasks are bound to; never browser-chosen."""
    profile: str
    bot_id: str
    user_id: int

    @property
    def key(self):
        return f"{self.profile}:tg{self.bot_id}:{self.user_id}"


def cursor_key(cursor):
    """A stored activity cursor as a comparable (last_active, session id) key.

    A bare timestamp from older state compares below every id at that time, so its ties are
    offered again rather than skipped.
    """
    if isinstance(cursor, list):
        return (cursor[0], cursor[1])
    return (cursor, "")


def clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


class TalkStore:
    def __init__(self, history, clock, voice_session_ttl_s, summary_chars):
        self.history = history
        self.clock = clock
        self.ttl = voice_session_ttl_s
        self.summary_chars = summary_chars
        self.lock = threading.Lock()

    def _binding_key(self, account):
        return f"hermes_talk:binding:{account.key}"

    def _conv_key(self, conversation_id):
        return f"hermes_talk:conversation:{conversation_id}"

    def _load(self, conversation_id):
        raw = self.history.get_meta(self._conv_key(conversation_id))
        try:
            state = json.loads(raw) if raw else None
        except ValueError:
            state = None
        return state if isinstance(state, dict) else None

    def _save(self, conversation_id, state):
        self.history.set_meta(self._conv_key(conversation_id), json.dumps(state, sort_keys=True))

    def _create(self, account):
        conversation_id = self.history.create_session(TITLE, SOURCE)
        state = {"account": account.key, "created": self.clock(), "voice_sessions": {},
                 "summary": "", "activity_cursor": None, "last_visit": None}
        self._save(conversation_id, state)
        self.history.set_meta(self._binding_key(account), conversation_id)
        return conversation_id, state

    def current(self, account):
        """The account's bound conversation, created on first use or when the bound row is gone."""
        with self.lock:
            conversation_id = self.history.get_meta(self._binding_key(account))
            state = self._load(conversation_id) if conversation_id else None
            if (not conversation_id or state is None or state.get("account") != account.key
                    or not self.history.session_exists(conversation_id)):
                if state is not None and state.get("account") == account.key:
                    self.history.set_meta(self._conv_key(conversation_id), "null")  # its row is gone
                conversation_id, state = self._create(account)
            self._expire_voice_sessions(conversation_id, state)
            return conversation_id, state

    def new(self, account, conversation_id):
        """An explicit new conversation; the previous one stays intact and unbound."""
        with self.lock:
            self._owned(account, conversation_id, bound=True)
            return self._create(account)

    def _owned(self, account, conversation_id, bound=False):
        if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
            raise TalkError(400, "Malformed conversation id.")
        state = self._load(conversation_id)
        if state is None or state.get("account") != account.key or not self.history.session_exists(conversation_id):
            raise TalkError(404, "That conversation is not available. Reopen Talk from the bot.")
        if bound and self.history.get_meta(self._binding_key(account)) != conversation_id:
            raise TalkError(409, "That conversation is no longer current. Reopen Talk from the bot.")
        return state

    def owned(self, account, conversation_id, bound=False):
        with self.lock:
            return self._owned(account, conversation_id, bound)

    def _expire_voice_sessions(self, conversation_id, state):
        now = self.clock()
        changed = False
        for record in state["voice_sessions"].values():
            if record.get("ended") is None and now - record["started"] > self.ttl:
                record["ended"], record["end_reason"] = now, "expired"
                changed = True
        if changed:
            state["summary"] = self._summary(conversation_id)
            self._save(conversation_id, state)

    def start_voice_session(self, account, conversation_id, briefing_watermark, job_ids=(), job_numbers=None):
        with self.lock:
            state = self._owned(account, conversation_id, bound=True)
            self._expire_voice_sessions(conversation_id, state)
            voice_session_id = "vs_" + secrets.token_urlsafe(18)
            state["generations"] = state.get("generations", 0) + 1
            state["voice_sessions"][voice_session_id] = {
                "started": self.clock(), "ended": None, "end_reason": None,
                "briefing_watermark": briefing_watermark, "spoke": False,
                "generation": state["generations"], "job_ids": list(job_ids), "snapshots": {},
                "job_numbers": dict(job_numbers or {})}
            # Bounded ledger: the oldest ended records go first.
            ended = sorted((r["started"], k) for k, r in state["voice_sessions"].items() if r["ended"] is not None)
            for _, key in ended[: max(0, len(state["voice_sessions"]) - 50)]:
                del state["voice_sessions"][key]
            state["last_visit"] = self.clock()
            self._save(conversation_id, state)
            return voice_session_id

    def _voice_session(self, state, voice_session_id):
        if not isinstance(voice_session_id, str) or not VOICE_SESSION_ID.fullmatch(voice_session_id):
            raise TalkError(400, "Malformed voice session id.")
        record = state["voice_sessions"].get(voice_session_id)
        if record is None:
            raise TalkError(404, "That voice session does not belong to this conversation.")
        return record

    def ingest(self, account, conversation_id, voice_session_id, events, batch_max, text_chars):
        """Append settled turns once: (accepted ids, duplicates included and never rewritten; the newly stored
        turns as (event id, sequence, role, text, interrupted) for item-level evidence; a copy of the call record)."""
        if not isinstance(events, list) or not 1 <= len(events) <= batch_max:
            raise TalkError(400, f"Send between 1 and {batch_max} events.")
        parsed = []
        for event in events:
            keys = set(event) if isinstance(event, dict) else set()
            if not {"id", "sequence", "role", "text"} <= keys <= {"id", "sequence", "role", "text", "interrupted"}:
                raise TalkError(400, "Each event needs exactly id, sequence, role and text.")
            if not isinstance(event["id"], str) or not EVENT_ID.fullmatch(event["id"]):
                raise TalkError(400, "Malformed event id.")
            if type(event["sequence"]) is not int or not 0 <= event["sequence"] < 1_000_000:
                raise TalkError(400, "Malformed event sequence.")
            if event["role"] not in ROLES:
                raise TalkError(400, "Only settled user and assistant text is accepted.")
            if "interrupted" in event and (type(event["interrupted"]) is not bool or event["role"] != "assistant"):
                raise TalkError(400, "Only an assistant event may be marked interrupted, as true or false.")
            text = event["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > text_chars:
                raise TalkError(400, f"Event text must be 1..{text_chars} characters.")
            parsed.append(event)
        if len({e["id"] for e in parsed}) != len(parsed):
            raise TalkError(400, "Duplicate event ids in one batch.")
        with self.lock:
            state = self._owned(account, conversation_id)
            record = self._voice_session(state, voice_session_id)
            fresh, turns = [], []
            for event in sorted(parsed, key=lambda e: e["sequence"]):
                receipt = f"talk:{voice_session_id}:{event['id']}"
                if not self.history.has_receipt(conversation_id, receipt):
                    fresh.append({"role": event["role"], "content": event["text"].strip(),
                                  "platform_message_id": receipt, "timestamp": self.clock()})
                    turns.append((event["id"], event["sequence"], event["role"], event["text"].strip(),
                                  bool(event.get("interrupted"))))
            if fresh:
                self.history.append(conversation_id, fresh)
            if any(m["role"] == "assistant" for m in fresh) and not record["spoke"]:
                record["spoke"] = True
                self._save(conversation_id, state)
            return [e["id"] for e in parsed], turns, dict(record)

    def voice_session(self, account, conversation_id, voice_session_id, live=False):
        """A copy of the account's voice session record; live=True also requires the current, unended call."""
        with self.lock:
            state = self._owned(account, conversation_id, bound=live)
            self._expire_voice_sessions(conversation_id, state)
            record = self._voice_session(state, voice_session_id)
            if live and record["ended"] is not None:
                raise TalkError(409, "That call has ended. Reopen Talk from the bot.")
            return dict(record)

    def save_snapshot(self, account, conversation_id, voice_session_id, cards, job_ids, inputs=()):
        """Record a mid-call context update; returns its revision. Nothing counts as read until it is acked."""
        with self.lock:
            state = self._owned(account, conversation_id, bound=True)
            record = self._voice_session(state, voice_session_id)
            snapshots = record.setdefault("snapshots", {})
            revision = record.get("revision", 0) + 1
            record["revision"] = revision
            snapshots[str(revision)] = {"cards": list(cards), "job_ids": list(job_ids), "inputs": list(inputs),
                                        "acked": False}
            for old in sorted(snapshots, key=int)[:-5]:
                del snapshots[old]
            self._save(conversation_id, state)
            return revision

    def ack_snapshot(self, account, conversation_id, voice_session_id, revision):
        """Mark a context update appended: (job ids, Pulse cards, chat inputs it carried); a repeated ack changes
        nothing."""
        with self.lock:
            state = self._owned(account, conversation_id)
            record = self._voice_session(state, voice_session_id)
            snapshot = (record.get("snapshots") or {}).get(str(revision)) if type(revision) is int else None
            if snapshot is None:
                raise TalkError(404, "That context update is not available.")
            if snapshot["acked"]:
                return [], [], []
            snapshot["acked"] = True
            self._save(conversation_id, state)
            return snapshot["job_ids"], snapshot.get("cards") or [], snapshot.get("inputs") or []

    def agent_session(self, conversation_id):
        session_id = self.history.get_meta(f"hermes_talk:agent_session:{conversation_id}")
        return session_id if session_id and self.history.session_exists(session_id) else None

    def bind_agent_session(self, conversation_id, session_id):
        self.history.set_meta(f"hermes_talk:agent_session:{conversation_id}", session_id)

    def end(self, account, conversation_id, voice_session_id):
        """Idempotent checkpoint: the first end closes the call; every end refreshes the summary."""
        with self.lock:
            state = self._owned(account, conversation_id)
            record = self._voice_session(state, voice_session_id)
            if record["ended"] is None:
                record["ended"], record["end_reason"] = self.clock(), "ended"
            state["summary"] = self._summary(conversation_id)
            self._save(conversation_id, state)
            return {"ok": True}

    def _summary(self, conversation_id):
        limit = self.summary_chars
        """A bounded extractive checkpoint: the latest user requests, no model call involved."""
        asks = [clip(text, 160) for role, text in self.history.recent(conversation_id, 40) if role == "user"]
        out = []
        for ask in reversed(asks):
            line = f"- {ask}"
            if sum(len(x) + 1 for x in out) + len(line) > limit:
                break
            out.insert(0, line)
        return "\n".join(out)
