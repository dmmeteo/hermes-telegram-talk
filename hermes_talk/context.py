"""Voice instructions for one call, built from bounded, labeled sections.

Every section has its own character budget and the whole text has a total
budget; sections are clipped, never silently dropped mid-word without a
marker. Skills are guidance text only. They register no tools.
"""
from .store import clip

FRAME = (
    "You are the realtime voice of the Hermes profile \"{name}\" in Hermes Talk, a Telegram Mini App. "
    "Speak naturally and briefly. When interrupted, stop and listen.\n"
    "Voice limits in this build: you cannot run tools, start or cancel Hermes work, read files or "
    "browse. If asked to do something, say plainly that Talk cannot start work yet and that nothing "
    "was executed. Never claim a task ran, finished or was sent. Ending this call cancels nothing."
)


def _section(title, body, limit):
    body = (body or "").strip()
    if not body or limit <= 0:
        return ""
    if len(body) > limit:
        body = body[: max(0, limit - 12)].rstrip() + "\n[clipped]"
    return f"## {title}\n{body}"


def compose(config, soul, skills, summary, turns, briefing):
    """Instruction text plus a per-section report of what was included (names and sizes only)."""
    b = config.budgets
    skill_parts, used = [], 0
    for name, text in skills:
        room = min(b.skill_chars, b.skills_chars - used)
        part = _section(f"Skill: {name}", text, room)
        if not part:
            break
        skill_parts.append(part)
        used += len(part)
    recent = "\n".join(f"{role}: {clip(text, b.turn_chars)}" for role, text in turns[-b.recent_turns:]) \
        if b.recent_turns else ""
    greeting = ""
    if config.greeting_enabled:
        greeting = (config.greeting + " Speak first only after the call connects. If the activity briefing "
                    "lists items, mention at most the two most relevant in one sentence and do not "
                    "imply anything is done unless its status says so.")
    sections = [
        ("frame", FRAME.format(name=config.display_name)),
        ("language", _section("Language", config.language_hint, 200)),
        ("soul", _section("Profile persona (SOUL)", soul, b.soul_chars)),
        ("skills", "\n\n".join(skill_parts)),
        ("summary", _section("Earlier in this conversation", summary, b.summary_chars)),
        ("recent", _section("Most recent turns", recent, b.recent_turns * (b.turn_chars + 12))),
        ("activity", _section("Activity since the last visit", briefing, b.activity_chars)),
        ("greeting", _section("Greeting", greeting, 800)),
    ]
    text, report = "", {}
    for key, part in sections:
        if not part:
            report[key] = 0
            continue
        room = b.instructions_chars - len(text) - 2
        if room <= 0:
            report[key] = 0
            continue
        part = part if len(part) <= room else part[: max(0, room - 12)].rstrip() + "\n[clipped]"
        text = f"{text}\n\n{part}" if text else part
        report[key] = len(part)
    report["skills_included"] = [name for name, _ in skills][: len(skill_parts)]
    return text, report
