"""Pulse candidates read from the profile's own sessions: who may hear about a row and its truthful status.

Status comes only from what the store recorded: a failed-turn row or an
error end is "failed"; a recently active open session is "active"; a stored
assistant reply is "replied" (delivery to a chat platform is not proven);
anything else, including stale open sessions, is "unknown". Nothing is
reported as done unless a reply row exists.

Rows are scanned by (last_active, session id). What was read is no longer a
cursor: each row becomes a Pulse card (pulse.py) whose own evidence says whether
it was exposed, mentioned or acknowledged.
"""
from datetime import datetime, timezone

from .store import clip, cursor_key  # noqa: F401  (cursor_key is part of this module's surface)

FAILED_END_REASONS = ("error", "failed", "crashed", "timeout")
ACTIVE_WINDOW_S = 30 * 60
NOT_EXHAUSTIVE = ("- More sessions changed than this briefing scans in one visit; older updates since the last "
                  "visit were not read and are not listed.")


def key(item):
    return (item.get("last_active") or 0, item["id"])


def status(row, now):
    if row.get("last_kind") == "failed_turn" or (row.get("end_reason") or "") in FAILED_END_REASONS:
        return "failed"
    if row.get("ended_at") is None and now - (row.get("last_active") or 0) < ACTIVE_WINDOW_S:
        return "active"
    if row.get("last_role") == "assistant":
        return "replied"
    return "unknown"


def unread(rows, *, since, user_id, owner_user_id, sources, exclude_ids):
    """Rows after the *since* key that this user may hear about, oldest first.

    A session tagged with a chat user belongs to that user only. An untagged one (CLI, cron,
    kanban) is the operator's and is shown only to the configured owner.
    """
    out = []
    for row in rows:
        if key(row) <= since or row["id"] in exclude_ids or row.get("source") not in sources:
            continue
        owner = row.get("user_id")
        mine = str(owner) == str(user_id) if owner not in (None, "") else user_id == owner_user_id
        if mine:
            out.append(row)
    return sorted(out, key=key)


def title(row):
    return clip(row.get("title") or row.get("preview") or "untitled", 80)


def when(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
