"""Talk Pulse: cards, importance, selection, attribution and feedback, as pure decisions.

Adapted for voice from Hermes Conduit notifier Pulse (MIT, LICENSES/CONDUIT-LICENSE): candidates
become fingerprinted cards, a selection picks the most important unread ones for a call, and every
card keeps separate evidence for each step a briefing goes through. The persistent per-account
record of those steps is pulse_ledger.py.

Item states, in order of evidence:
- candidate: known to Pulse, never supplied to the voice model.
- exposed: supplied to the voice model whole (call instructions or an acknowledged context update).
- mentioned: a settled, uninterrupted assistant turn of that call referred to this card. The text
  was produced; nobody has confirmed hearing it.
- acknowledged: a settled user turn followed that mention in the same call, or the user gave
  feedback on the card. Only this ends a card's revision.
- snoozed: the user asked to hear it later; it is a candidate again once its follow-up is due.
- dismissed, superseded, duplicate: closed without being acknowledged.
"""
import hashlib
import re
import unicodedata

UNREAD = ("candidate", "exposed", "mentioned")
CLOSED = ("acknowledged", "dismissed", "superseded", "duplicate")
STATES = UNREAD + ("snoozed",) + CLOSED

BASE_IMPORTANCE = {"failed": 0.9, "replied": 0.6, "active": 0.5, "unknown": 0.3}
FOLLOWUP_BOOST = 0.3
UNCONFIRMED_BOOST = 0.05
FATIGUE_STEP, FATIGUE_MAX = 0.05, 0.2
PREFERENCE_MAX = 0.3
TOPIC_MAX = 0.15
DUPLICATE_WINDOW_S = 10 * 60
DELIVERY_NOTES = {"push": "lead with this", "screen": "only if there is room or the user asks what else",
                  "brief": "held for the brief"}

FEEDBACK = {"useful": "useful", "not useful": "not_useful", "dismiss": "dismiss", "later": "later",
            "more": "more"}
FEEDBACK_REQUEST = re.compile(r"(?i)^\s*pulse\s+(?:feedback\s+)?(?:card\s+)?p\s*(\d{1,3})\s*[:,-]?\s*"
                              r"(useful|not\s+useful|dismiss|later|more)(?:\s*[:,-]?\s*(?:because\s+|reason\s*[:=]?\s*)?"
                              r"(not\s+relevant|already\s+knew|too\s+late|too\s+often|wrong\s+time))?\s*[.!?]?\s*$")
REASON_KEYS = {"not relevant": "not_relevant", "already knew": "already_knew", "too late": "too_late",
               "too often": "too_often", "wrong time": "wrong_time"}
STOPWORDS = frozenset("a an and are as at be by for from in is it of on or the to was were with".split())
NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
TASK_MENTION = re.compile(r"\btask\s*(?:number\s*)?#?\s*([0-9]{1,4}|[a-z]+)\b")


def tokens(text):
    text = unicodedata.normalize("NFKD", str(text)).lower()
    words = re.findall(r"[a-z0-9]+", "".join(c for c in text if not unicodedata.combining(c)))
    return [NUMBER_WORDS.get(w, w) for w in words if w not in STOPWORDS]


def fingerprint(kind, ref, status, title):
    """One revision of one source item: the same session in the same state with the same title is the same card."""
    raw = "\x1f".join((kind, str(ref), str(status), " ".join(tokens(title))))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def content_key(status, title):
    return f"{status}:{' '.join(tokens(title))}"


def preference(memory, source, title):
    """Learned weight from earlier feedback on the same source and title words, bounded either way."""
    counts = (memory.get("sources") or {}).get(source) or {}
    up, down = counts.get("useful", 0), counts.get("not_useful", 0) + counts.get("dismiss", 0)
    weight = PREFERENCE_MAX * 2 * (up - down) / (up + down + 2)
    topics = memory.get("topics") or {}
    words = set(tokens(title))
    topic = sum(topics.get(w, 0.0) for w in words) / max(1, len(words))
    return max(-PREFERENCE_MAX, min(PREFERENCE_MAX, weight)) + max(-TOPIC_MAX, min(TOPIC_MAX, topic))


def importance(item, memory, now):
    score = BASE_IMPORTANCE.get(item["status"], BASE_IMPORTANCE["unknown"])
    followup = item.get("followup") or {}
    if followup.get("due") is not None and followup["due"] <= now and followup.get("state") != "done":
        score += FOLLOWUP_BOOST
    if item["state"] == "mentioned":
        score += UNCONFIRMED_BOOST
    unheard = item.get("exposures", 0) - item.get("mentions", 0)
    score -= min(FATIGUE_MAX, FATIGUE_STEP * max(0, unheard))
    return round(score + preference(memory, item.get("source"), item.get("title", "")), 4)


def eligible(item, now):
    if item["state"] in UNREAD:
        return True
    due = (item.get("followup") or {}).get("due")
    return item["state"] == "snoozed" and due is not None and due <= now


def select(items, memory, now, limit):
    """(chosen, waiting): the most important eligible cards first; ties go to the oldest, so a backlog drains in order."""
    pool = [i for i in items if eligible(i, now)]
    pool.sort(key=lambda i: (-importance(i, memory, now), i["at"], i["ref"]))
    return pool[:limit], len(pool)


def mentioned(text, card, others):
    """Whether an assistant turn refers to this card: its whole title, or most of its words including one that
    tells it apart from every other card the call carries."""
    said = tokens(text)
    words = tokens(card["title"])
    if not words:
        return False
    phrase = " ".join(words)
    if f" {phrase} " in f" {' '.join(said)} ":
        return True
    present = set(said)
    hits = [w for w in words if w in present]
    if len(hits) * 5 < len(words) * 3:
        return False
    shared = set(words)
    for other in others:
        shared &= set(tokens(other["title"]))
    return not others or any(w not in shared for w in hits)


def tasks_mentioned(text, numbers):
    """Task numbers an assistant turn names ("task 3", "task three"), restricted to the ones the call carried."""
    found = set()
    for match in TASK_MENTION.finditer(" ".join(tokens(text))):
        value = NUMBER_WORDS.get(match.group(1), match.group(1))
        if value.isdigit() and int(value) in numbers:
            found.add(int(value))
    return found


def parse_feedback(request):
    """(card number, feedback kind) for a "pulse P<n> <kind>" delegation, else None."""
    match = FEEDBACK_REQUEST.match(request or "")
    if match is None:
        return None
    return int(match.group(1)), FEEDBACK[" ".join(match.group(2).lower().split())]


def feedback_reason(request):
    """The reason a "pulse P<n> <kind> <reason>" delegation gives (pulse_policy.REASONS), else None."""
    match = FEEDBACK_REQUEST.match(request or "")
    if match is None or not match.group(3):
        return None
    return REASON_KEYS[" ".join(match.group(3).lower().split())]


def line(card, handle, when):
    notes = []
    if (card.get("followup") or {}).get("due") is not None and card["state"] == "snoozed":
        notes.append("follow-up the user asked for")
    elif card.get("follows"):
        notes.append("follow-up on an earlier update")
    if card["state"] == "mentioned":
        notes.append("mentioned before, not confirmed heard")
    pick = card.get("pick") or {}
    if pick.get("why"):
        notes.append(f"why now: {pick['why']}")
    delivery = (card.get("delivery") or {}).get("level")
    if delivery in DELIVERY_NOTES:
        notes.append(DELIVERY_NOTES[delivery])
    note = f" ({'; '.join(notes)})" if notes else ""
    if card.get("kind") == "followup":
        return f"- {handle} [followup] {card['title']} (Pulse memory item {card['ref']}, {when}){note}"
    if card.get("kind") == "claim":
        return f"- {handle} [claim] {card['title']} (Pulse memory item {card['ref']}, {when}){note}"
    return f"- {handle} [{card['status']}] {card['title']} ({card['source']} session {card['ref']}, {when}){note}"
