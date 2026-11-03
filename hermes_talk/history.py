"""Two implementations of the one history surface Talk uses.

NativeHistory is Hermes' own state.db through public SessionDB methods (no
SQL here). MemoryHistory is a synthetic in-process stand-in with the same
methods, for offline checks and the frontend fixture; it is never used by a
real deployment.
"""
import itertools
import threading
import time
from pathlib import Path

ROLES = ("user", "assistant")
ACTIVITY_FIELDS = ("id", "source", "title", "preview", "started_at", "ended_at", "end_reason", "last_active", "user_id")
PAGE = 50
# Pages overlap so a session that jumps to the top mid-scan cannot push an unread one past a page edge unseen.
PAGE_OVERLAP = 5


def _turns(rows):
    return [(r["role"], r["content"]) for r in rows if r["role"] in ROLES and r.get("content")]


def _rows(rows):
    out = []
    for r in rows:
        calls = r.get("tool_calls")
        if isinstance(calls, str):
            try:
                import json
                calls = json.loads(calls)
            except ValueError:
                calls = []
        out.append({"role": r.get("role"), "content": r.get("content"), "tool_name": r.get("tool_name"),
                    "tool_call_id": r.get("tool_call_id"), "tool_calls": calls if isinstance(calls, list) else []})
    return out


class NativeHistory:
    def __init__(self, hermes_home):
        from hermes_state import SessionDB
        self.db = SessionDB(db_path=Path(hermes_home) / "state.db")
        self.lock = threading.Lock()

    def close(self):
        self.db.close()

    def create_session(self, title, source):
        from hermes_state_ids import new_session_id
        with self.lock:
            session_id = new_session_id()
            self.db.create_session(session_id, source)
            # Titles are unique per profile; a clash takes the id's tail.
            for candidate in (title, f"{title} ({session_id[-6:]})"):
                try:
                    self.db.set_session_title(session_id, candidate)
                    break
                except ValueError:
                    continue
            return session_id

    def session_exists(self, session_id):
        with self.lock:
            return self.db.get_session(session_id) is not None

    def get_meta(self, key):
        with self.lock:
            return self.db.get_meta(key)

    def set_meta(self, key, value):
        with self.lock:
            self.db.set_meta(key, value)

    def list_meta_prefix(self, prefix):
        with self.lock:
            return self.db.list_meta_prefix(prefix)

    def has_receipt(self, session_id, receipt):
        with self.lock:
            return self.db.has_platform_message_id(session_id, receipt)

    def append(self, session_id, messages):
        with self.lock:
            for m in messages:
                self.db.append_message(session_id, m["role"], content=m["content"],
                                       platform_message_id=m["platform_message_id"], timestamp=m["timestamp"])

    def recent(self, session_id, limit):
        with self.lock:
            return _turns(self.db.get_messages(session_id, limit=limit, latest=True))

    def rows(self, session_id, limit):
        """The session's latest stored rows with their tool calls (role, content, tool_name, tool_call_id,
        tool_calls), oldest first: what Pulse judges Hermes's claims against (pulse_claims)."""
        with self.lock:
            return _rows(self.db.get_messages(session_id, limit=limit, latest=True))

    def activity_rows(self, exclude_source, since_ts, max_rows):
        """(rows active at or after since_ts, newest first; exhaustive) read page by page.

        exhaustive is False when max_rows was reached before the scan got back to since_ts.
        """
        rows, seen, offset = [], set(), 0
        with self.lock:
            while True:
                page = self.db.list_sessions_rich(limit=PAGE, offset=offset, order_by_last_active=True,
                                                  exclude_sources=[exclude_source])
                for row in page:
                    if row.get("last_active") is None or row["id"] in seen:
                        continue  # an undated row says nothing about where the scan is in time
                    if row["last_active"] < since_ts:
                        return rows, True
                    if len(rows) >= max_rows:
                        return rows, False
                    seen.add(row["id"])
                    rows.append({k: row.get(k) for k in ACTIVITY_FIELDS})
                if len(page) < PAGE:
                    return rows, True
                offset += PAGE - PAGE_OVERLAP

    def last_turn(self, session_id):
        with self.lock:
            last = self.db.get_messages(session_id, limit=1, latest=True)
        return (last[-1]["role"], last[-1].get("display_kind")) if last else (None, None)


class MemoryHistory:
    def __init__(self):
        self.sessions, self.messages, self.meta = {}, {}, {}
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    def create_session(self, title, source, **fields):
        with self.lock:
            session_id = f"mem_{next(self.ids):06d}"
            now = time.time()
            self.sessions[session_id] = {"id": session_id, "source": source, "title": title, "preview": None,
                                         "started_at": now, "ended_at": None, "end_reason": None,
                                         "last_active": now, "user_id": None, **fields}
            self.messages[session_id] = []
            return session_id

    def session_exists(self, session_id):
        return session_id in self.sessions

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = value

    def list_meta_prefix(self, prefix):
        return [(k, v) for k, v in self.meta.items() if k.startswith(prefix)]

    def has_receipt(self, session_id, receipt):
        return any(m.get("platform_message_id") == receipt for m in self.messages.get(session_id, []))

    def append(self, session_id, messages):
        with self.lock:
            for m in messages:
                self.messages[session_id].append(dict(m))
                self.sessions[session_id]["last_active"] = m.get("timestamp") or time.time()

    def recent(self, session_id, limit):
        return _turns(self.messages.get(session_id, [])[-limit:])

    def rows(self, session_id, limit):
        return _rows(self.messages.get(session_id, [])[-limit:])

    def end_session(self, session_id, reason, at=None):
        self.sessions[session_id].update(ended_at=at or time.time(), end_reason=reason)

    def activity_rows(self, exclude_source, since_ts, max_rows):
        rows = sorted((dict(s) for s in self.sessions.values()
                       if s["source"] != exclude_source and s["last_active"] >= since_ts),
                      key=lambda s: s["last_active"], reverse=True)
        return rows[:max_rows], len(rows) <= max_rows

    def last_turn(self, session_id):
        last = self.messages[session_id][-1] if self.messages.get(session_id) else None
        return (last["role"], last.get("display_kind")) if last else (None, None)
