"""Voice instructions for one call, built from bounded, labeled sections.

The frame (voice limits) and the greeting rules are placed first and always fit. The other
sections fill the remaining room in priority order, each within its own character budget,
headers and the "[clipped]" marker included. Skills are guidance text only. They register no tools.
"""
from .store import clip
from .config import SELF_REFERENCE

FRAME = (
    "You are the realtime voice of the Hermes profile \"{name}\" in Hermes Telegram Talk, a Telegram Mini App. "
    "Speak naturally and briefly. When interrupted, stop and listen.\n"
    "Voice limits in this build: you cannot run tools, start or cancel Hermes work, read files or "
    "browse. If asked to do something, say plainly that Talk cannot start work yet and that nothing "
    "was executed. Never claim a task ran, finished or was sent. Ending this call cancels nothing."
)
TASKS_FRAME = (
    "You are the realtime voice of the Hermes profile \"{name}\" in Hermes Telegram Talk, a Telegram Mini App. "
    "Speak naturally and briefly. When interrupted, stop and listen.\n"
    "You can hand work to Hermes, this profile's own agent with its tools. When the user asks for something this "
    "context cannot answer (look something up, read or check something, research), delegate it. A delegated task "
    "runs in the background: say you handed it to Hermes and keep talking. Its result arrives later on that "
    "delegation or as context. Report only what it says, name the task by its number (\"task 3\") when you "
    "report it, and never claim a task ran, finished or was sent before then. To cancel a task, delegate exactly \"cancel task <number>\". Hermes cannot ask anyone to approve an "
    "action during a task; such an action is blocked and reported. Ending this call cancels nothing; open tasks "
    "keep running."
)
MARKER = "\n[clipped]"
MIN_SECTION = 40
ACTIVITY_HEADER = "## Activity since the last visit\n"
TASKS_HEADER = "## Hermes tasks\n"
UPDATE_HEADER = "## Context update {revision}\n"
CHAT_HEADER = ("## Chat input\n"
               "The user sent these in the Telegram chat this call continues, during the call, oldest first. Treat "
               "each as said by the user now and answer briefly. A line that says you cannot see or read something "
               "means you did not receive it: say so plainly and never guess its content.\n")
PULSE_RULES = (
    "## Pulse updates\n"
    "Activity lines are Pulse updates P1, P2 and so on. Name an update by its title, never by its label. Unmentioned "
    "updates stay unread and return next time. When the user reacts to one, delegate exactly \"pulse P<n> useful\", "
    "\"not useful\", \"dismiss\", \"later\" or \"more\" and say the reply; when they say why, add it: \"not relevant\", "
    "\"already knew\", \"too late\", \"too often\" or \"wrong time\". Bring up lines marked \"lead with this\" first."
)


def _fit(text, limit):
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(MARKER))].rstrip() + MARKER


def _section(title, body, limit):
    body = (body or "").strip()
    header = f"## {title}\n"
    if not body or limit < len(header) + MIN_SECTION:
        return ""
    return header + _fit(body, limit - len(header))


def _activity(lines, total, note, limit):
    """(section, k): the first k item lines whole, never a partial one, plus an honest tail."""
    for k in range(len(lines), -1, -1):
        rest = total - k
        tail = []
        if rest:
            tail.append(f"- {rest} more update(s) not listed yet; they will be offered next time." if k else
                        f"- {rest} unread update(s) are not listed yet; they will be offered next time.")
        if note:
            tail.append(note)
        if not lines[:k] + tail:
            return "", 0
        text = ACTIVITY_HEADER + "\n".join(lines[:k] + tail)
        if len(text) <= limit:
            return text, k
    return "", 0


def whole_lines(header, items, limit):
    """(section, ids): leading (id, line, synopsis) items under header within limit.

    An item whose line does not fit whole goes in as its synopsis (a bounded excerpt plus how to
    retrieve the rest) instead of blocking every item behind it; an item that fits neither way
    stops the section, and a tail says how many are still waiting.
    """
    taken, text = [], header
    for index, (item_id, line, synopsis) in enumerate(items):
        fitted = next((c for c in (line, synopsis) if c and len(text + c) <= limit), None)
        if fitted is None:
            tail = f"- {len(items) - index} more task update(s) are waiting and follow next time."
            if len(text + tail) <= limit:
                text += tail + "\n"
            break
        text += fitted + "\n"
        taken.append(item_id)
    return (text.rstrip("\n") if taken else ""), taken


CLIP_NOTE = " [... {n} more characters of this input were not carried into the call]"


def chat_section(lines, limit):
    """(section, n): leading whole chat-input lines within limit; the rest wait for the next update.

    A first line that alone exceeds the limit is carried clipped, with an explicit note of how much was left out,
    so one oversized input (an album of text files) cannot starve every later message."""
    out, n = CHAT_HEADER, 0
    for line in lines:
        if len(out) + len(line) + 1 > limit:
            if n == 0:
                room = limit - len(out) - len(CLIP_NOTE) - 24
                if room > 0:
                    out += line[:room] + CLIP_NOTE.format(n=len(line) - room) + "\n"
                    n = 1
            break
        out += line + "\n"
        n += 1
    return (out.rstrip() if n else ""), n


def update(revision, job_items, activity_lines, activity_total, activity_note, limit):
    """A mid-call context update: (text, job ids carried whole, activity lines carried whole)."""
    header = UPDATE_HEADER.format(revision=revision)
    jobs, job_ids = whole_lines(TASKS_HEADER, job_items, limit - len(header))
    room = limit - len(header) - len(jobs) - 2
    activity, k = _activity(activity_lines, activity_total, activity_note, room) if room >= MIN_SECTION else ("", 0)
    body = "\n\n".join(part for part in (jobs, activity) if part)
    return (header + body if body else ""), job_ids, k


def compose(config, soul, skills, summary, turns, activity_lines, activity_total, activity_note,
            tasks_ready=False, job_items=(), reports_note="", greeting_line=None):
    """Instruction text plus a per-section report of what was included (names and sizes only).

    report["activity_items"] is how many of activity_lines the text carries whole; only those count
    as read. report["job_ids"] lists the task results the text carries whole.
    """
    b = config.budgets
    frame = (TASKS_FRAME + reports_note if tasks_ready else FRAME).format(name=config.display_name)
    greeting = ""
    if config.greeting_enabled and greeting_line:
        greeting = _section("Greeting", "Speak first only after the call connects, and open with exactly this one "
                            f"short line, in its own language: \"{greeting_line}\" Then stop and listen. Do not add "
                            "a briefing or list updates in the greeting; bring them up once the user has spoken.", 800)
    elif config.greeting_enabled:
        greeting = _section("Greeting", config.greeting + " Speak first only after the call connects. If the "
                            "activity briefing lists items, mention at most the two most relevant in one sentence "
                            "and do not imply anything is done unless its status says so.", 800)
    skill_parts, used = [], 0
    for name, text in skills:
        part = _section(f"Skill: {name}", text, min(b.skill_chars, b.skills_chars - used))
        if not part:
            break
        skill_parts.append((name, part))
        used += len(part) + 2
    recent = "\n".join(f"{role}: {clip(text, b.turn_chars)}" for role, text in turns[-b.recent_turns:]) \
        if b.recent_turns else ""
    middle = [
        ("language", _section("Language", " ".join(filter(None, (
            config.language_hint, SELF_REFERENCE.get(config.self_reference, "")))), 200 if not config.self_reference
            else 400)),
        ("soul", _section("Profile persona (SOUL)", soul, b.soul_chars)),
        ("skills", "\n\n".join(part for _, part in skill_parts)),
        ("summary", _section("Earlier in this conversation", summary, b.summary_chars)),
        ("recent", _section("Most recent turns", recent, b.recent_turns * (b.turn_chars + 12) + 20)),
    ]
    room = b.instructions_chars - len(frame) - (len(greeting) + 2 if greeting else 0)
    parts, report = [frame], {"frame": len(frame)}
    for key, part in middle:
        if part and room - 2 >= MIN_SECTION:
            part = _fit(part, room - 2)
            parts.append(part)
            room -= len(part) + 2
        else:
            part = ""
        report[key] = len(part)
    jobs, report["job_ids"] = "", []
    if job_items and room - 2 >= MIN_SECTION:
        jobs, report["job_ids"] = whole_lines(TASKS_HEADER, job_items, min(b.tasks_chars, room - 2))
        if jobs:
            parts.append(jobs)
            room -= len(jobs) + 2
    report["jobs"] = len(jobs)
    activity, report["activity_items"] = "", 0
    if room - 2 >= MIN_SECTION:
        activity, report["activity_items"] = _activity(activity_lines, activity_total, activity_note,
                                                       min(b.activity_chars, room - 2))
    report["pulse_rules"] = 0
    if activity:
        parts.append(activity)
        room -= len(activity) + 2
        if report["activity_items"] and room - 2 >= len(PULSE_RULES):
            parts.append(PULSE_RULES)
            report["pulse_rules"] = len(PULSE_RULES)
    report["activity"] = len(activity)
    if greeting:
        parts.append(greeting)
    report["greeting"] = len(greeting)
    report["skills_included"] = [name for name, _ in skill_parts] if report["skills"] else []
    return "\n\n".join(parts), report
