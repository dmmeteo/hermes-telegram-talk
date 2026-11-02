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


def snapshot(rows, *, now, since, user_id, owner_user_id, sources, exclude_ids, limit):
    """(items, unlisted): unread items oldest first, so a cut never skips one past the cursor.

    A session tagged with a chat user belongs to that user only. An untagged one (CLI, cron,
    kanban) is the operator's and is shown only to the configured owner.
    """
    unread = []
    for row in rows:
        last_active = row.get("last_active") or 0
        if last_active <= since or row["id"] in exclude_ids or row.get("source") not in sources:
            continue
        owner = row.get("user_id")
        mine = str(owner) == str(user_id) if owner not in (None, "") else user_id == owner_user_id
        if not mine:
            continue
        unread.append({"id": row["id"], "source": row["source"], "status": status(row, now),
                       "title": clip(row.get("title") or row.get("preview") or "untitled", 80),
                       "started_at": row.get("started_at"), "last_active": last_active})
    unread.sort(key=lambda item: item["last_active"])
    return unread[:limit], len(unread) - min(len(unread), limit)


def briefing(items, unlisted=0):
    lines = []
    for item in items:
        when = datetime.fromtimestamp(item["last_active"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"- [{item['status']}] {item['title']} ({item['source']} session {item['id']}, {when})")
    if unlisted:
        lines.append(f"- {unlisted} newer update(s) not listed yet; they will be offered next time.")
    return "\n".join(lines)
