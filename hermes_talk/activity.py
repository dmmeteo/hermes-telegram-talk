"""A small Pulse-style briefing read from the profile's own sessions.

Status comes only from what the store recorded: a failed-turn row or an
error end is "failed"; a recently active open session is "active"; a stored
assistant reply is "replied" (delivery to a chat platform is not proven);
anything else, including stale open sessions, is "unknown". Nothing is
reported as done unless a reply row exists.

Items are ordered by (last_active, session id), oldest first. The read cursor
is the key of the last complete item the final instructions carried, so an
item that was cut, omitted or tied on time is offered again rather than skipped.
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


def line(row, now):
    when = datetime.fromtimestamp(row["last_active"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = clip(row.get("title") or row.get("preview") or "untitled", 80)
    return f"- [{status(row, now)}] {title} ({row['source']} session {row['id']}, {when})"
