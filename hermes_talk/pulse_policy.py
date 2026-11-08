"""Pulse delivery policy: how loudly, and whether now, a selected card comes up in a voice call.

Ported from Conduit notifier at d25391f6 (MIT, LICENSES/CONDUIT-NOTIFIER-LICENSE):
- pulse_learning.py: the loudness ladder (push, screen, brief, off) as per-kind ceilings, DEFAULT_LEVELS,
  the rating reasons (REASONS) and the lessons they leave: "too often" steps the kind's ceiling one level
  quieter, "too late" makes the kind count as due sooner (SOONER, MAX_SOONER), "already knew" (SEEN) is
  told to the selection model, "wrong time" closes that part of the day for the kind's pushes (PART),
  and "not relevant" is a rule that turns off what comes from that place (RULE).
- pulse_prefs.py (pulse_prefs here): the learned relevance model; a candidate's score (learning, the
  model's value, its pick) against SURFACE_AT and PUSH_AT decides its level (pulse_prefs.level).
- pulse_quiet.py: quiet weekdays and a quiet spell (``quiet_until``); while quiet nothing comes up on
  its own and it is held for the first brief after.
- pulse_hours.py: the night and the parts of the day; in the night only what cannot wait (overdue or
  due within SOON_S) comes up, the rest is held for the morning.
- pulse_brief.py: what was held comes with the brief, once, and is stamped ``briefed``. Talk stamps the
  day's brief and a card's ``briefed`` only when a brief card is acknowledged (heard, ADR 0001), never when
  it is chosen or put in the instructions; until then the brief stays open, and a card of the day's brief
  that is still unread is released to the screen of later calls that day (``owed``).

Talk adaptation: Talk speaks only when the user opens a call, so there is no push channel and nothing
scheduled. "push" leads the call's opening briefing, "screen" is carried after the leads (said if
there is room or the user asks what else), "brief" is held and comes with the first call after the
night or quiet ends (the brief), "off" is not brought up. Settings come from the plugin config
(pulse.zone, pulse.night, pulse.quiet_days, pulse.quiet_until, pulse.nudge); Talk has no settings UI.
The night is off unless configured, because a call is always the user's own choice to listen.
"""
from datetime import datetime, timezone

from . import pulse_prefs as prefs

LEVELS = prefs.LEVELS
LEVEL_RANK = {level: n for n, level in enumerate(LEVELS)}  # push 0 .. off 3
DEFAULT_LEVELS = {"followup": "push", "activity": "push", "claim": "screen"}
REASONS = ("not_relevant", "already_knew", "too_late", "too_often", "wrong_time")
SOONER, MAX_SOONER = 0.5, 2
LEVEL, RULE, SOONER_LESSON, SEEN, PART = "level", "rule", "sooner", "seen", "part"
MORNING, MIDDAY, EVENING, NIGHT = "morning", "midday", "evening", "night"
DEFAULT_BOUNDS = ((22, 0), (7, 0))
NUDGES = ("gentle", "balanced", "eager")
MAX_SIGNALS = 400
MAX_LESSONS = 100
URGENT = (prefs.OVERDUE, prefs.SOON)


class Settings:
    """The account's delivery settings (config ``pulse``): zone, night ((h, m), (h, m)) or None, quiet weekdays
    (Monday 0), quiet_until epoch or None, nudge level."""

    def __init__(self, zone="UTC", night=None, quiet_days=(), quiet_until=None, nudge="balanced"):
        self.zone, self.night, self.quiet_days = zone, night, tuple(quiet_days)
        self.quiet_until, self.nudge = quiet_until, nudge if nudge in NUDGES else "balanced"

    def tz(self):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.zone)
        except Exception:  # an unknown zone reads as UTC rather than failing a call
            return timezone.utc

    def local(self, at):
        return datetime.fromtimestamp(at, self.tz())


def _minute(settings, at):
    local = settings.local(at)
    return local.hour * 60 + local.minute, local.weekday()


def _within(minute, start, end):
    return start <= minute < end if start < end else minute >= start or minute < end


def part(settings, at):
    """The part of the day ``at`` is in, named by the user's night (the default one when none is set)."""
    (sh, sm), (eh, em) = settings.night or DEFAULT_BOUNDS
    minute, _ = _minute(settings, at)
    if _within(minute, sh * 60 + sm, eh * 60 + em):
        return NIGHT
    if _within(minute, eh * 60 + em, 12 * 60):
        return MORNING
    if _within(minute, 12 * 60, 17 * 60):
        return MIDDAY
    return EVENING


def is_night(settings, at):
    return settings.night is not None and part(settings, at) == NIGHT


def quiet_why(settings, at):
    """Why Pulse keeps to itself now: "until" (a quiet spell), "day" (a quiet weekday), or None."""
    if settings.quiet_until is not None and at < settings.quiet_until:
        return "until"
    _, weekday = _minute(settings, at)
    return "day" if weekday in settings.quiet_days else None


def brief_day(settings, at):
    return settings.local(at).strftime("%Y-%m-%d")


# --- what a card is, for learning --------------------------------------------------------------------
def lesson_kind(card):
    return card.get("kind", "activity") if card.get("kind") in ("followup", "claim") else "activity"


def card_kind(card):
    if card.get("kind") == "followup":
        return card.get("memory_kind") or "followup"
    if card.get("kind") == "claim":
        return "claim"
    return card.get("status") or "unknown"


def card_place(card):
    return "area:memory" if card.get("kind") == "followup" else prefs.place("", "", source=str(card.get("source")))


def card_due(card, memory):
    if card.get("kind") == "followup":
        item = (memory.get("items") or {}).get(str(card.get("ref", ""))[1:])
        if item and item.get("due"):
            return item["due"]
    followup = card.get("followup") or {}
    return followup.get("due") if card.get("state") == "snoozed" else None


# --- signals and lessons ---------------------------------------------------------------------------
def signal(memory, card, kind, now):
    """Record the learning signal a voice feedback leaves (one row per card and kind of signal, newest wins)."""
    if kind == "useful":
        what, useful, weight = prefs.THUMB, 1.0, 1.0
    elif kind in ("not_useful", "dismiss"):
        what, useful, weight = prefs.THUMB, 0.0, 1.0
    elif kind == "more":
        what, useful, weight = prefs.ACT, 1.0, prefs.ACT_SENT
    else:
        return None  # "later" leaves none, as Not now upstream
    band = prefs.band(card_due(card, memory), now)
    row = {"card_id": card["fp"], "what": what, "useful": useful, "weight": weight, "kind": card_kind(card),
           "place": card_place(card), "band": band, "at": now, "pushed": None}
    signals = memory.setdefault("signals", {})
    signals[f"{card['fp']}:{what}"] = row
    for key in sorted(signals, key=lambda k: signals[k]["at"])[: max(0, len(signals) - MAX_SIGNALS)]:
        del signals[key]
    return row


def ceiling(memory, kind):
    """The kind's level, a ceiling: its newest level lesson, else its default."""
    lessons = [l for l in memory.get("lessons") or [] if l["what"] == LEVEL and l["kind"] == kind]
    return lessons[-1]["value"] if lessons else DEFAULT_LEVELS.get(kind, "push")


def pace(memory, kind):
    count = sum(1 for l in memory.get("lessons") or [] if l["what"] == SOONER_LESSON and l["kind"] == kind)
    return SOONER ** min(MAX_SOONER, count)


def closed_parts(memory, kind):
    return {l["value"] for l in memory.get("lessons") or [] if l["what"] == PART and l["kind"] == kind}


def muted(memory, place_):
    return any(l["what"] == RULE and l["value"] == place_ for l in memory.get("lessons") or [])


def seen_titles(memory):
    return [l["title"] for l in memory.get("lessons") or [] if l["what"] == SEEN][-6:]


def reason(memory, card, why, settings, now):
    """The lesson a rating's reason leaves (pulse_learning.rate); None when one like it is in force already."""
    kind = lesson_kind(card)
    lessons = memory.setdefault("lessons", [])
    if why == "too_often":
        current = ceiling(memory, kind)
        quieter = LEVELS[min(len(LEVELS) - 1, LEVEL_RANK[current] + 1)]
        if quieter == current:
            return None
        lesson = {"what": LEVEL, "kind": kind, "value": quieter, "cause": "too often"}
    elif why == "too_late":
        if pace(memory, kind) <= SOONER ** MAX_SOONER:
            return None
        lesson = {"what": SOONER_LESSON, "kind": kind, "value": SOONER}
    elif why == "already_knew":
        lesson = {"what": SEEN, "kind": kind, "title": card["title"]}
    elif why == "wrong_time":
        value = part(settings, now)
        if value in closed_parts(memory, kind):
            return None
        lesson = {"what": PART, "kind": kind, "value": value}
    elif why == "not_relevant":
        value = card_place(card)
        if muted(memory, value):
            return None
        lesson = {"what": RULE, "kind": kind, "value": value}
    else:
        return None
    lesson.update(at=now, card=card["fp"])
    lessons.append(lesson)
    del lessons[: max(0, len(lessons) - MAX_LESSONS)]
    return lesson


# --- the decision ----------------------------------------------------------------------------------
def decide(card, memory, learned, settings, now):
    """{"level", "score", "band", "why"} for one selected card at ``now``."""
    kind = lesson_kind(card)
    band = prefs.band(card_due(card, memory), now, pace(memory, kind))
    attrs = prefs.attributes(card_kind(card), card_place(card), band)
    pick = card.get("pick") or {}
    picked = pick.get("decision") in (None, "surfaced", "over_limit")  # the fallback ranking counts as a pick
    score = prefs.score(learned, attrs, value=pick.get("value") or None, picked=picked)
    level = prefs.level(score, band, nudge=settings.nudge, lowest="brief" if kind == "followup" else "off")
    level = prefs.quieter(level, ceiling(memory, kind))
    why = "learned" if level in ("brief", "off") and learned.votes else "level"
    if muted(memory, attrs["place"]):
        level, why = "off", "not relevant rule"
    elif level == "push" and part(settings, now) in closed_parts(memory, kind):
        level, why = "screen", "wrong time"
    quiet = quiet_why(settings, now)
    if level in ("push", "screen") and (quiet or (is_night(settings, now) and band not in URGENT)):
        level, why = "brief", f"held: {'quiet ' + quiet if quiet else 'night'}"
    factor = learned.factor(attrs)
    return {"level": level, "score": round(score, 3), "band": band, "why": why,
            "learned": prefs.factor_words(factor) if factor else ""}


def arrange(cards, memory, settings, now, last_brief, owed=()):
    """(delivered, held, off, brief): the cards a call carries, loudest first (push, then held cards when this
    call is the brief, then screen), and the ones held or turned off, each card carrying its ``delivery``.
    ``owed`` are the fingerprints of the day's heard brief: still unread, they are released to the screen."""
    learned = prefs.learn(list((memory.get("signals") or {}).values()), now)
    quiet = quiet_why(settings, now)
    open_ = not quiet and not is_night(settings, now)
    is_brief = open_ and last_brief != brief_day(settings, now)
    push, screen, briefed, held, off = [], [], [], [], []
    for card in cards:
        decision = decide(card, memory, learned, settings, now)
        was = card.get("delivery") or {}
        if (decision["level"] in ("push", "screen") and str(was.get("why", "")).startswith("held")
                and not card.get("briefed")):
            # Held for the night or a quiet spell: the first brief after takes it (pulse_brief); on a day whose
            # brief already went it is released to the screen, as upstream's release_held at midnight.
            decision = dict(decision, level="brief" if is_brief else "screen",
                            why=was["why"] if is_brief else "released after the brief")
        elif decision["level"] == "brief" and not is_brief and open_ and card.get("fp") in owed:
            # In the day's heard brief but not heard itself (clipped, interrupted, never named): still owed,
            # released to the screen like a held card rather than waiting for tomorrow's brief.
            decision = dict(decision, level="screen", why="released after the brief")
        card = dict(card, delivery=decision)
        if decision["level"] == "push":
            push.append(card)
        elif decision["level"] == "screen":
            screen.append(card)
        elif decision["level"] == "brief" and is_brief:
            card["delivery"] = dict(decision, level="brief", why="the brief")
            briefed.append(card)
        elif decision["level"] == "brief":
            held.append(card)
        else:
            off.append(card)
    return push + briefed + screen, held, off, is_brief and bool(briefed)


def settings_from(config):
    pulse = getattr(config, "pulse", None)
    if pulse is None:
        return Settings()
    return Settings(pulse.zone, pulse.night, pulse.quiet_days, pulse.quiet_until, pulse.nudge)


__all__ = ["DEFAULT_LEVELS", "REASONS", "Settings", "arrange", "decide", "reason", "signal"]
