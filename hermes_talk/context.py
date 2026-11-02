"""Voice instructions for one call, built from bounded, labeled sections.

The frame (voice limits) and the greeting rules are placed first and always fit. The other
sections fill the remaining room in priority order, each within its own character budget,
headers and the "[clipped]" marker included. Skills are guidance text only. They register no tools.
"""
from .store import clip

FRAME = (
    "You are the realtime voice of the Hermes profile \"{name}\" in Hermes Talk, a Telegram Mini App. "
    "Speak naturally and briefly. When interrupted, stop and listen.\n"
    "Voice limits in this build: you cannot run tools, start or cancel Hermes work, read files or "
    "browse. If asked to do something, say plainly that Talk cannot start work yet and that nothing "
    "was executed. Never claim a task ran, finished or was sent. Ending this call cancels nothing."
)
MARKER = "\n[clipped]"
MIN_SECTION = 40


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


def compose(config, soul, skills, summary, turns, briefing):
    """Instruction text plus a per-section report of what was included (names and sizes only)."""
    b = config.budgets
    frame = FRAME.format(name=config.display_name)
    greeting = ""
    if config.greeting_enabled:
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
        ("language", _section("Language", config.language_hint, 200)),
        ("soul", _section("Profile persona (SOUL)", soul, b.soul_chars)),
        ("skills", "\n\n".join(part for _, part in skill_parts)),
        ("summary", _section("Earlier in this conversation", summary, b.summary_chars)),
        ("recent", _section("Most recent turns", recent, b.recent_turns * (b.turn_chars + 12) + 20)),
        ("activity", _section("Activity since the last visit", briefing, b.activity_chars)),
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
    if greeting:
        parts.append(greeting)
    report["greeting"] = len(greeting)
    report["skills_included"] = [name for name, _ in skill_parts] if report["skills"] else []
    return "\n\n".join(parts), report
