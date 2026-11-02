"""A small Pulse-style briefing read from the profile's own sessions.

Status comes only from what the store recorded: a failed-turn row or an
error end is "failed"; a recently active open session is "active"; a stored
assistant reply is "replied" (delivery to a chat platform is not proven);
anything else, including stale open sessions, is "unknown". Nothing is
reported as done unless a reply row exists.
"""
from datetime import datetime, timezone

from .store import clip

FAILED_END_REASONS = ("error", "failed", "crashed", "timeout")
ACTIVE_WINDOW_S = 30 * 60


def status(row, now):
    if row.get("last_kind") == "failed_turn" or (row.get("end_reason") or "") in FAILED_END_REASONS:
        return "failed"
    if row.get("ended_at") is None and now - (row.get("last_active") or 0) < ACTIVE_WINDOW_S:
        return "active"
    if row.get("last_role") == "assistant":
        return "replied"
    return "unknown"


def snapshot(rows, *, now, since, user_id, sources, exclude_ids, limit):
    """Items newer than *since* that belong to this user (or to no chat user), newest first."""
    items = []
    for row in rows:
        last_active = row.get("last_active") or 0
        if last_active <= since or row["id"] in exclude_ids or row.get("source") not in sources:
            continue
        if row.get("user_id") not in (None, "", str(user_id)):
            continue
        items.append({"id": row["id"], "source": row["source"], "status": status(row, now),
                      "title": clip(row.get("title") or row.get("preview") or "untitled", 80),
                      "started_at": row.get("started_at"), "last_active": last_active})
        if len(items) >= limit:
            break
    return items


def briefing(items):
    lines = []
    for item in items:
        when = datetime.fromtimestamp(item["last_active"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"- [{item['status']}] {item['title']} ({item['source']} session {item['id']}, {when})")
    return "\n".join(lines)
