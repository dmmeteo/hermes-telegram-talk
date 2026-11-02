"""Hermes Talk runtime: the authenticated operations behind the /tg-voice/ API.

Every method takes a Telegram user id that server.py already verified from
signed initData and the allowlist. Profile and bot come from configuration
and the bot token, never from the request.
"""
import json
import re
import time

from . import activity
from .context import compose
from .store import SOURCE, Account, TalkError, TalkStore

SECRETS = [re.compile(p) for p in (
    r"\bsk-[A-Za-z0-9_-]{16,}",
    r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"(?i)\b(api[_-]?key|token|secret|password|bearer)\b\s*[:=]?\s*[A-Za-z0-9_\-./+]{12,}",
)]


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
    def __init__(self, config, bot_token, history, soul=native_soul, skill=native_skill, clock=time.time):
        self.config = config
        self.bot_id = bot_token.split(":", 1)[0]
        self.history = history
        self.store = TalkStore(history, clock, config.voice_session_ttl_s)
        self.soul = soul
        self.skill = skill
        self.clock = clock

    def account(self, user_id):
        return Account(self.config.profile_id, self.bot_id, user_id)

    def _public(self, conversation_id, avatar_url):
        c = self.config
        return {"conversation_id": conversation_id,
                "profile": {"id": c.profile_id, "display_name": c.display_name, "avatar_url": avatar_url},
                "appearance": {"listen_color": c.listen_color, "speak_color": c.speak_color},
                "greeting_enabled": c.greeting_enabled,
                "limits": {"event_batch_max": c.budgets.event_batch_max}}

    def bootstrap(self, user_id, avatar_url):
        conversation_id, _ = self.store.current(self.account(user_id))
        return self._public(conversation_id, avatar_url)

    def new(self, user_id, conversation_id, avatar_url):
        conversation_id, _ = self.store.new(self.account(user_id), conversation_id)
        return self._public(conversation_id, avatar_url)

    def _briefing(self, user_id, conversation_id, state):
        c = self.config
        if not c.activity_enabled or c.budgets.activity_items == 0:
            return "", None
        now = self.clock()
        since = state.get("activity_cursor") or now - c.activity_window_s
        try:
            rows = self.history.activity_rows(SOURCE, max(20, c.budgets.activity_items * 4))
        except Exception as exc:  # a briefing is never worth the call
            return f"(Activity could not be read: {type(exc).__name__}. Say so if asked; do not guess.)", None
        items = activity.snapshot(rows, now=now, since=since, user_id=user_id, sources=c.activity_sources,
                                  exclude_ids={conversation_id}, limit=c.budgets.activity_items)
        watermark = max((i["last_active"] for i in items), default=None)
        return activity.briefing(items), watermark

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
        briefing, watermark = self._briefing(user_id, conversation_id, state)
        instructions, report = compose(c, soul, [s for s in skills if s[1]], redact(state.get("summary") or ""),
                                       turns, redact(briefing))
        return conversation_id, instructions, watermark, report

    def session_config(self, instructions):
        return {"model": self.config.model, "instructions": instructions,
                "audio": {"output": {"voice": self.config.voice}}, "delegation": {"type": "client"}}

    def started(self, user_id, conversation_id, watermark):
        return self.store.start_voice_session(self.account(user_id), conversation_id, watermark)

    def events(self, user_id, conversation_id, voice_session_id, events):
        b = self.config.budgets
        accepted = self.store.ingest(self.account(user_id), conversation_id, voice_session_id, events,
                                     b.event_batch_max, b.event_text_chars)
        return {"accepted_ids": accepted, "conversation_id": conversation_id}

    def end(self, user_id, conversation_id, voice_session_id):
        return self.store.end(self.account(user_id), conversation_id, voice_session_id,
                              self.config.budgets.summary_chars)


__all__ = ["TalkRuntime", "TalkError", "redact"]
