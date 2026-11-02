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


def _turns(rows):
    return [(r["role"], r["content"]) for r in rows if r["role"] in ROLES and r.get("content")]


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

    def activity_rows(self, exclude_source, limit):
        with self.lock:
            rows = self.db.list_sessions_rich(limit=limit, order_by_last_active=True,
                                              exclude_sources=[exclude_source])
            out = []
            for row in rows:
                last = self.db.get_messages(row["id"], limit=1, latest=True)
                out.append({**{k: row.get(k) for k in ("id", "source", "title", "preview", "started_at",
                                                       "ended_at", "end_reason", "last_active", "user_id")},
                            "last_role": last[-1]["role"] if last else None,
                            "last_kind": last[-1].get("display_kind") if last else None})
            return out


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

    def has_receipt(self, session_id, receipt):
        return any(m.get("platform_message_id") == receipt for m in self.messages.get(session_id, []))

    def append(self, session_id, messages):
        with self.lock:
            for m in messages:
                self.messages[session_id].append(dict(m))
                self.sessions[session_id]["last_active"] = m.get("timestamp") or time.time()

    def recent(self, session_id, limit):
        return _turns(self.messages.get(session_id, [])[-limit:])

    def end_session(self, session_id, reason, at=None):
        self.sessions[session_id].update(ended_at=at or time.time(), end_reason=reason)

    def activity_rows(self, exclude_source, limit):
        rows = sorted((s for s in self.sessions.values() if s["source"] != exclude_source),
                      key=lambda s: s["last_active"], reverse=True)[:limit]
        out = []
        for row in rows:
            last = self.messages[row["id"]][-1] if self.messages[row["id"]] else None
            out.append({**row, "last_role": last["role"] if last else None,
                        "last_kind": last.get("display_kind") if last else None})
        return out
