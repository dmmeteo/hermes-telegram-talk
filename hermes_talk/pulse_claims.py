"""Hermes's claims, judged against the tool calls of their own turn.

Ported from Conduit notifier pulse_claims.py (ACTIONS, MAX_CLAIMS, KEEP_S, TURN_CALLS, SHOWN_CALLS,
CHANGES, CRON_TOOLS, CRON_CHANGES, RUNS_OTHERS, NAMED, READS, REMOVES, does, turn, judge) and
pulse_memory.py _claim (the "claims" part of the memory reading) at d25391f6 (MIT,
LICENSES/CONDUIT-NOTIFIER-LICENSE).

Memory also notes what Hermes said in a session it had just done with a tool: set a reminder,
added or changed a calendar event, created or changed a scheduled job, or sent a message. Code then
looks for the call behind it: one that worked, in the same session and the same turn (from the
user's message before the claim to their next one), of a tool that could do that (``does``).

Talk adaptation: upstream fills an outcome ledger from post_tool_call hooks in every process. Talk
reads the evidence the session already keeps: the assistant's tool_calls and the tool rows of the
claim's turn, through SessionDB's public get_messages (history.rows). No hook writes, no core change,
nothing outside the account's own sessions. A call counts as worked unless its tool row says it
failed. Conduit's scheduled-call tool (conduit_call_watch) does not exist on this surface. Upstream
keeps claims to judge before showing them; Talk keeps every judged claim and turns an unbacked one
into a "claim" card the selection model may bring up ("Hermes said it set a reminder, but no call
made one"), held below push by default (pulse_policy.DEFAULT_LEVELS).
"""
import json
import re

ACTIONS = ("reminder", "event", "job", "message")
MAX_CLAIMS = 6  # a chunk's
KEEP_S = 30 * 86400.0
TURN_CALLS = 500
SHOWN_CALLS = 12
CHANGES = {
    "reminder": ("add_reminder", "update_reminder", "set_alarm", "set_timer"),
    "event": ("add_event", "update_event"),
    "job": (),
    "message": (),
}
CRON_TOOLS = ("cronjob", "cronjob_manage")
CRON_CHANGES = ("create", "update", "resume")
RUNS_OTHERS = ("delegate_task", "execute_code", "terminal", "process", "process_manage")
NAMED = {
    "reminder": ("remind", "alarm", "timer", "todo", "task", "cron", "schedule"),
    "event": ("event", "calendar", "meeting"),
    "job": ("cron", "schedule", "job"),
    "message": ("send", "message", "mail", "email", "sms", "reply", "notify"),
}
READS = ("list", "get", "search", "read", "find", "fetch", "query", "view", "lookup", "status")
REMOVES = ("delete", "remove", "cancel", "clear", "unset")
_WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
INSTRUCTIONS = """

"claims" lists what Hermes said in this conversation it had just done with one of its tools, at most 6: {"action": "reminder" (it set a reminder, an alarm or a timer), "event" (it added or changed a calendar event), "job" (it created or changed a scheduled job) or "message" (it sent a message or an email), "text": one short sentence in the third person ("Hermes set a reminder for 5pm to call the dentist."), "evidence": the exact words that say so, copied from one of Hermes's lines, at most 200 characters, "line": that line's number}. Only Hermes's own lines saying it is done, whether or not a tool line shows it; not that it will do it, offers to, asks first or couldn't."""


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _norm(text):
    return " ".join(str(text or "").lower().split())


def claim(raw, rows):
    """Something Hermes said it had just done, in the words of one of its numbered lines, else None."""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    text = _clip(raw.get("text"), 300)
    evidence = _clip(raw.get("evidence"), 300).strip(" \"'“”‘’")
    if action not in ACTIONS or not text or not evidence:
        return None
    try:
        number = int(raw.get("line"))
    except (TypeError, ValueError):
        number = None
    candidates = [r for r in rows if r["role"] == "assistant" and _norm(evidence) in _norm(r["text"])]
    row = next((r for r in candidates if r["line"] == number), candidates[0] if candidates else None)
    if row is None:
        return None  # not something Hermes said
    return {"action": action, "text": text, "evidence": evidence, "line": row["line"]}


def parse(parsed, rows):
    found, said = [], set()
    raw = parsed.get("claims") if isinstance(parsed.get("claims"), list) else []
    for item in (claim(r, rows) for r in raw[:MAX_CLAIMS]):
        if item and (item["line"], item["action"]) not in said:  # once per line and action
            said.add((item["line"], item["action"]))
            found.append(item)
    return found


def _status(content):
    try:
        data = json.loads(content) if isinstance(content, str) and content.strip().startswith("{") else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        if data.get("error") or data.get("success") is False or str(data.get("status", "")).lower() in (
                "error", "failed"):
            return "error", _clip(data.get("error") or data.get("status"), 120)
        return "ok", ""
    text = str(content or "").strip().lower()
    return ("error", _clip(content, 120)) if text.startswith(("error", "failed")) else ("ok", "")


def outcomes(rows):
    """The tool calls in session rows (history.rows, oldest first) as upstream ledger outcomes, with the row index."""
    calls = {}
    for row in rows:
        for call in row.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                calls[call.get("id")] = str(args.get("action") or "") if isinstance(args, dict) else ""
    out = []
    for index, row in enumerate(rows):
        if row["role"] != "tool" or not row.get("tool_name"):
            continue
        status, error = _status(row.get("content"))
        out.append({"index": index, "tool": row["tool_name"], "action": calls.get(row.get("tool_call_id"), ""),
                    "status": status, "error": error})
    return out


def does(outcome, action):
    """Whether an outcome's call could have done ``action`` (ACTIONS)."""
    tool = str(outcome.get("tool") or "").strip().lower()
    act = str(outcome.get("action") or "").strip().lower()
    if tool in RUNS_OTHERS:
        return True
    if tool in CRON_TOOLS:
        return action in ("reminder", "job") and act in CRON_CHANGES
    if act in CHANGES[action]:
        return True
    words = [word.lower() for word in _WORD.findall(f"{outcome.get('tool') or ''} {outcome.get('action') or ''}")]
    return (any(word.startswith(NAMED[action]) for word in words)
            and not any(word.startswith(READS + REMOVES) for word in words))


def line(outcome):
    parts = [outcome["tool"]] + ([outcome["action"]] if outcome.get("action") else [])
    result = outcome["status"] + (f": {outcome['error']}" if outcome["status"] == "error" and outcome["error"] else "")
    return f"{' '.join(parts)} -> {result}"


def turn(rows, index):
    """(first, last) row indexes of the turn row ``index`` is in: from the user's message before it to the row
    before their next one; None when no user message comes before it."""
    start = next((i for i in range(index - 1, -1, -1) if rows[i]["role"] == "user"), None)
    if start is None:
        return None
    end = next((i - 1 for i in range(index + 1, len(rows)) if rows[i]["role"] == "user"), len(rows) - 1)
    return start, end


def judge(found, rows, now):
    """Each claim with the call that backed it ("" for none) and its turn's calls; a claim whose words are not on
    an assistant row of ``rows`` is left out."""
    calls = outcomes(rows)
    judged = []
    for item in found:
        index = next((i for i, r in enumerate(rows) if r["role"] == "assistant"
                      and _norm(item["evidence"]) in _norm(r.get("content"))), None)
        bounds = turn(rows, index) if index is not None else None
        if bounds is None:
            continue
        inside = [c for c in calls if bounds[0] <= c["index"] <= bounds[1]][:TURN_CALLS]
        backing = next((c for c in inside if c["status"] == "ok" and does(c, item["action"])), None)
        shown = [line(c) for c in inside[:SHOWN_CALLS]]
        if len(inside) > SHOWN_CALLS:
            shown.append(f"(and {len(inside) - SHOWN_CALLS} more)")
        judged.append({**item, "backed_by": line(backing) if backing else "", "calls": shown, "kept_at": now})
    return judged


def prune(claims, now):
    return [c for c in claims if now - c.get("kept_at", now) < KEEP_S]


__all__ = ["ACTIONS", "INSTRUCTIONS", "does", "judge", "outcomes", "parse", "prune", "turn"]
