"""One live Telegram status message per Hermes task, sent once and then edited through the Bot API.

The message is a status mark and one short English sentence naming the task's objective: queued, running,
completed, failed or cancelled. It never carries the result, which stays in the native task session and reaches the
user by voice. The title is the voice model's TITLE line when it gave a usable one, or the request itself when that is
already a short English sentence. Otherwise the profile's own model, through the plugin's host ctx.llm, names the
objective from the work the task's turn admitted to T3 (work.py intents, newest last) and the request (a speech
transcript, often with filler): first before the only send, then, while it has no usable answer (the route is not
bound yet after a Talk start, an outage, an unusable answer), again with backoff up to TITLE_ATTEMPTS times in all;
a later answer renames the message with one edit. Until then the message shows the admitted T3 start's own title when
it is a usable one, else "Hermes task N". It never shows the speech's own words: a transcript's opening is filler,
not an objective (docs/adr/0013-objective-task-titles.md).

A task's record (title, bound destination, status to show, message identity) is stored in the profile's
state.db (SessionDB meta) before anything is sent. The first send is claimed (pending -> sending, stored),
so a crash or restart never sends it blindly again: a claim left by an earlier process becomes "uncertain".
Telegram's sendMessage has no idempotency key, so a timed-out or otherwise ambiguous send is "uncertain" too
and never resent; such a task gets no further updates. Only failures that prove nothing was sent (refused
connection, rate limit) go back to pending, at most `attempts` times. Once the message exists, every later
status is an editMessageText of that message id, with its own `attempts`. An edit is idempotent, so a lost or
ambiguous one is simply repeated. One Telegram refuses (the message is gone) is held until the next status; one
that kept failing for a temporary reason is held too, and a restart tries it again.

The default destination is the private chat between the authenticated user and the bot whose signed
initData opened the call. A configured named destination replaces it when the user asked for it explicitly.
"""
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, fields, replace

from . import jobs
from .agent import process_start

logger = logging.getLogger("tg-voice")

PENDING, SENDING, SENT, FAILED, UNCERTAIN = "pending", "sending", "sent", "failed", "uncertain"
HIDDEN = "hidden"  # a task its account does not show; inert for every desk version (nothing to send or edit)
TRANSITIONS = {PENDING: {SENDING}, SENDING: {SENT, PENDING, FAILED, UNCERTAIN}}
STATUS = {jobs.ACCEPTED: "queued", jobs.RUNNING: "running", jobs.SUCCEEDED: "completed", jobs.FAILED: "failed",
          jobs.INTERRUPTED: "failed", jobs.CANCELLED: "cancelled"}
MARKS = {"queued": "🕓", "running": "⏳", "blocked": "❓", "completed": "✅", "failed": "❌", "cancelled": "⏹",
         "unknown": "❔"}
REPORT_PREFIX = "hermes_talk:report:"
SINCE_KEY = "hermes_talk:reports_since"
SEND_TIMEOUT_S = 30.0
BOT_API = "https://api.telegram.org"
MAX_REPLY_BYTES = 65536
NOT_SENT_RETRYABLE = ("connecterror", "connection refused", "too many requests", "retry after", "flood control")
NOT_SENT_FINAL = ("chat not found", "bot was blocked", "forbidden", "user is deactivated", "bad request",
                  "can't initiate conversation", "not configured", "message is too long")
# Edits held because Hermes' own sender broke on 2026-11-16 (core 471537ae3a), not because Telegram refused them.
OUTAGE_HOLDS = ("Hermes Telegram sender is not installed", "missing standalone_sender_fn")
EDIT_DONE, EDIT_RETRY, EDIT_FINAL = "done", "retry", "final"
EDIT_REFUSED = ("message to edit not found", "message can't be edited", "chat not found", "bot was blocked",
                "forbidden", "user is deactivated", "bad request", "not configured")

TITLE_LINE = re.compile(r"TITLE\s*:\s*(.*)")
TITLE_CHARS = 80
TITLE_WORDS = 10
TITLE_PURPOSE = "talk_task_title"
TITLE_TIMEOUT_S = 20.0
TITLE_ATTEMPTS = 5
TITLE_BACKOFF_S = (10.0, 20.0, 40.0, 60.0)  # between attempts; covers the host route's bind delay after a start
TITLE_INSTRUCTIONS = (
    "You write the title of a background task's one-line status message. Below is the work the assistant admitted "
    "for the task, if any, and the user's spoken request it came from: a speech transcript, possibly in another "
    "language, often opening with filler, acknowledgements or other topics. Reply with a JSON object "
    "{\"title\": \"...\"}: one short plain English sentence, an imperative of at most "
    f"{TITLE_WORDS} words ending with a period, stating the task's concrete objective, for example \"Add a dark theme "
    "to the settings page.\" When admitted work is given, name the objective of the newest admitted work. Name the "
    "goal, not the conversation: leave out filler, greetings and thinking aloud, and never translate the request's "
    "opening words. Do not claim it is done and do not add anything the text does not say. If no concrete objective "
    "is stated, reply {\"title\": \"\"}. No quotes, markup or emoji.")
# Markup characters stay out of a title: the message is plain text, sent and edited without a parse mode.
MARKUP = re.compile(r"[*_`\[\]~|<>#\\]")
DIRECTIVE = re.compile(r"(?im)^\s*report\s+to\s*:\s*(.+?)\s*$")
RETRY_AFTER = re.compile(r"(?i)retry (?:in|after) (\d+(?:\.\d+)?)")


class TransitionError(Exception):
    pass


def _plain(text):
    return re.sub(r" ([.,!?;:])", r"\1", " ".join(MARKUP.sub(" ", text).split()))


def _flat(text):
    return " ".join(str(text or "").split())


def _english(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isascii() for c in letters)


def usable_title(text):
    """A plain English title of a few words, whole (never cut), else ""."""
    title = _plain(text or "").strip("\"'“”‘’ ")
    return title if _english(title) and len(title.split()) <= TITLE_WORDS and len(title) <= TITLE_CHARS else ""


def take_title(request):
    """(request without its title line, the voice model's few-word English title or ""). Only a last line
    "TITLE: ..." (before or after a REPORT TO line) is the voice model's; any other line is the user's task."""
    lines = request.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not line or DIRECTIVE.fullmatch(line):
            continue
        match = TITLE_LINE.fullmatch(line)
        if match is None:
            return request, ""
        return "\n".join(lines[:i] + lines[i + 1:]).strip(), usable_title(match.group(1))
    return request, ""


def report_status(job):
    """What the message shows: a running job's T3 work may be queued in T3 or blocked on a question or approval."""
    if job.state == jobs.RUNNING and job.large:
        return job.work_status or "running"
    return "unknown" if job.unknown else STATUS[job.state]


def job_title(job, redact):
    """(title, final). A usable TITLE line, or a request that is itself a short English sentence, is final. Otherwise the
    title is provisional until the host model names the objective: the newest admitted T3 start's own title when it
    is usable, else the task's number. Never the request's words, whose opening is filler more often than not."""
    title = usable_title(redact(job.title)) if job.title else ""
    request = usable_title(redact(job.request))
    if title or request:
        return title or request, True
    starts = (usable_title(redact(link.intent_title)) for link in reversed(job.links)
              if link.counts and link.via == "start" and link.intent_title)
    return next((t for t in starts if t), "") or f"Hermes task {job.number}", False


def name_source(job, redact):
    """What the host model names a task from: the work its turn admitted to T3 (newest last), then the request. A send's
    own message is its intent; the thread's earlier title belongs to the work the thread was started for."""
    admitted = []
    for link in job.links:
        if not link.counts or not (link.intent or link.intent_title):
            continue
        if link.via == "start":
            head = f'- New T3 work titled "{_flat(redact(link.intent_title))}": ' if link.intent_title else "- New T3 work: "
        else:
            head = "- New work sent to an existing T3 thread: "
        admitted.append(head + _flat(redact(link.intent)))
    parts = ["Work the assistant admitted for this task, newest last:\n" + "\n".join(admitted)] if admitted else []
    return "\n\n".join(parts + ["The user's spoken request (a speech transcript):\n" + _flat(redact(job.request))])


def ask_title(model, source):
    """The host model's English title from name_source (pulse.model.PulseModel over ctx.llm); raises when it has none."""
    title = model.ask(TITLE_INSTRUCTIONS, source, TITLE_PURPOSE, max_tokens=1000, timeout=TITLE_TIMEOUT_S).get("title")
    return title if isinstance(title, str) else ""


@dataclass(frozen=True)
class Destination:
    """Where a job's status message goes; bound from authenticated server state when the job is accepted."""
    kind: str  # "private": the user's own chat with this bot; "named": a configured destination
    chat_id: int
    thread_id: int = None
    name: str = ""
    bot_id: str = ""

    @classmethod
    def private(cls, user_id, bot_id=""):
        return cls("private", user_id, bot_id=bot_id)

    def label(self):
        return "this Telegram chat" if self.kind == "private" else self.name


@dataclass(frozen=True)
class Report:
    """A task's one status message. `state` is whether the message exists; `status` is what it should show,
    `shown` what Telegram confirmed it shows, `held` a status whose edit Telegram refused or that gave up;
    `stalled` marks a give-up on temporary errors, which a restart retries. `naming` marks a provisional title the host
    model is still to name from `name_input` (stored, so a restart names from the same text): `name_tries` attempts so
    far, the next one not before `name_after`. While the first attempt runs before the first send (`holding`), `status`
    stays the first one and later statuses wait in order, so every step is still shown. A title named after the send
    clears `shown`: the message does not show the current text until the rename's edit lands."""
    job_id: str
    scope: str
    destination: dict
    title: str = ""
    status: str = ""  # "" only for a report stored by an earlier version, which is kept as is
    shown: str = None
    held: str = None
    stalled: bool = False
    naming: bool = False
    name_input: str = ""
    name_tries: int = 0
    name_after: float = 0.0
    state: str = PENDING
    attempts: int = 0
    not_before: float = 0.0
    message_id: str = None
    error: str = ""
    owner: str = None
    created: float = 0.0
    updated: float = 0.0
    notes: tuple = field(default_factory=tuple)

    def to(self, state, **changes):
        if state not in TRANSITIONS.get(self.state, ()):
            raise TransitionError(f"{self.state} -> {state}")
        return replace(self, state=state, **changes)

    @property
    def text(self):
        return f"{MARKS[self.status]} {self.title}"

    @property
    def holding(self):
        return self.naming and self.state == PENDING and self.name_tries == 0

    @property
    def renaming(self):
        return self.naming and self.state in (PENDING, SENT)

    @property
    def needs_edit(self):
        return self.state == SENT and self.status and self.shown != self.status and self.held != self.status

    def public(self):
        return {"state": self.state, "destination": Destination(**self.destination).label()}


FIELDS = {f.name for f in fields(Report)}


def report_key(job_id):
    return REPORT_PREFIX + job_id


def load(raw):
    try:
        data = json.loads(raw) if raw else None
        data = {k: v for k, v in data.items() if k in FIELDS}
        data["notes"] = tuple(data.get("notes") or ())
        return Report(**data)
    except (ValueError, TypeError, AttributeError):
        return None


def classify(result):
    """(state, message id, error) of a first send, from the sender's result dict."""
    if isinstance(result, dict) and result.get("success"):
        return SENT, str(result.get("message_id") or ""), "; ".join(result.get("warnings") or [])[:300]
    error = str((result or {}).get("error") if isinstance(result, dict) else result or "no result")[:300]
    lowered = error.lower()
    if any(m in lowered for m in NOT_SENT_FINAL):
        return FAILED, None, error
    if any(m in lowered for m in NOT_SENT_RETRYABLE):
        return PENDING, None, error
    return UNCERTAIN, None, error


def retry_after(error):
    """Telegram's own wait in a flood-control answer, in seconds, if it named one."""
    match = RETRY_AFTER.search(error or "")
    return float(match.group(1)) if match else None


def classify_edit(result):
    if isinstance(result, dict) and result.get("success"):
        return EDIT_DONE
    lowered = str((result or {}).get("error") if isinstance(result, dict) else result).lower()
    if "not modified" in lowered:  # the message already shows this text
        return EDIT_DONE
    return EDIT_FINAL if any(m in lowered for m in EDIT_REFUSED) else EDIT_RETRY


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # The token is part of the URL; a redirect must not carry the request anywhere else.
    def redirect_request(self, *args, **kwargs):
        return None


def _open(request, timeout):
    """One Bot API request. TELEGRAM_PROXY, when set, is its HTTP(S) proxy, as for Hermes' own Telegram calls."""
    proxy = os.environ.get("TELEGRAM_PROXY")
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    return urllib.request.build_opener(_NoRedirect, *handlers).open(request, timeout=timeout)


class TelegramSender:
    """The two Bot API calls a status message needs, sendMessage and editMessageText, as plain text with this
    Talk server's bot token, which Talk already holds and never stores. Talk makes them itself and imports
    nothing from Hermes, so a core update cannot break them (docs/adr/0014-status-message-bot-api-client.md).
    An error names Telegram's own description or the exception's type, never the request URL."""

    def __init__(self, bot_token, timeout=SEND_TIMEOUT_S):
        self.token = bot_token
        self.timeout = timeout

    def _foreign(self, destination):
        if destination.bot_id and destination.bot_id != self.token.split(":", 1)[0]:
            return {"error": "not configured: the report is bound to another bot than this server's"}
        return None

    def _call(self, method, fields):
        request = urllib.request.Request(f"{BOT_API}/bot{self.token}/{method}", data=json.dumps(fields).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            try:
                with _open(request, self.timeout) as response:
                    reply = json.loads(response.read(MAX_REPLY_BYTES))
            except urllib.error.HTTPError as refusal:
                with refusal:
                    try:
                        description = json.loads(refusal.read(MAX_REPLY_BYTES))["description"]
                    except Exception:
                        description = f"HTTP {refusal.code}"
                return {"error": f"Telegram {method} failed: {description}"}
            return {"success": True, "message_id": str(reply["result"]["message_id"])}
        except urllib.error.URLError as exc:
            reason = exc.reason
            # urllib wraps only failures before Telegram had the whole request: nothing was sent.
            if isinstance(reason, OSError) and not isinstance(reason, TimeoutError):
                return {"error": f"Telegram {method} failed: ConnectError: {type(reason).__name__}"}
            failure = reason if isinstance(reason, Exception) else exc
        except Exception as exc:
            failure = exc
        if isinstance(failure, TimeoutError):
            return {"error": f"{method} timed out after {self.timeout:.0f}s; it may still have been delivered"}
        return {"error": f"{method} raised {type(failure).__name__}; it may have been delivered"}

    def send(self, destination, text):
        refused = self._foreign(destination)
        if refused:
            return refused
        fields = {"chat_id": destination.chat_id, "text": text}
        # A forum's General topic is thread 1 inbound, but the Bot API refuses message_thread_id=1.
        if destination.thread_id not in (None, 1):
            result = self._call("sendMessage", {**fields, "message_thread_id": destination.thread_id})
            if "thread not found" not in str(result.get("error")).lower():
                return result
            # Telegram refused a deleted topic, so nothing was sent: the chat itself gets the message.
        return self._call("sendMessage", fields)

    def edit(self, destination, message_id, text):
        return self._foreign(destination) or self._call(
            "editMessageText", {"chat_id": destination.chat_id, "message_id": int(message_id), "text": text})


class ReportDesk:
    """Durable status messages: created once at a task's first status, claimed before the only send, then
    edited to each later status by one worker per task."""

    def __init__(self, history, sender, clock, attempts=3, backoff=(5.0, 30.0), redact=str, namer=None, visible=None,
                 name_backoff=TITLE_BACKOFF_S):
        self.history, self.sender, self.clock, self.redact, self.namer = history, sender, clock, redact, namer
        self.visible = visible  # job -> whether a job without a message gets one (notify.py); None: every job
        self.name_backoff = name_backoff
        self.passed = {}  # job id -> statuses it passed while naming, shown in order after the first send
        self.attempts, self.backoff = attempts, backoff
        self.owner = f"{os.getpid()}:{process_start(os.getpid())}:{secrets.token_hex(4)}"
        self.lock = threading.RLock()
        self.busy = set()
        # Tasks accepted before reports existed in this profile never get a message.
        self.since = float(history.get_meta(SINCE_KEY) or 0) or self.clock()
        history.set_meta(SINCE_KEY, repr(self.since))
        self.reports = {}
        for _, raw in history.list_meta_prefix(REPORT_PREFIX):
            report = load(raw)
            if report is not None:
                self.reports[report.job_id] = report

    def _save(self, report):
        report = replace(report, updated=self.clock())
        self.reports[report.job_id] = report
        self.history.set_meta(report_key(report.job_id), json.dumps(asdict(report), sort_keys=True))
        return report

    def get(self, job_id):
        """The task's status message record; None while it has none (never sent, or hidden)."""
        report = self.reports.get(job_id)
        return None if report is None or report.state == HIDDEN else report

    def follow(self, job):
        """Called with every stored version of a job: records its status and starts the message's update.

        A job its account does not show gets no message; once it has one (it was shown, or became large T3 work
        while large work is shown), the message follows it to the end."""
        status = report_status(job)
        with self.lock:
            report = self.reports.get(job.id)
            if report is not None and report.state == HIDDEN:
                if job.terminal or self.visible is None or not self.visible(job):
                    return None
                report = None  # its switch is on now and the task is still open: it gets its one message
            if report is None:
                if (job.created or 0) < self.since:
                    return None
                destination = job.report_to or asdict(Destination.private(
                    int(job.scope.rsplit(":", 1)[1]), job.scope.rsplit(":", 2)[1][2:]))
                if self.visible is not None and not self.visible(job):
                    # Stored so that this and every earlier desk version leave the task without a message.
                    self._save(Report(job_id=job.id, scope=job.scope, destination=destination, state=HIDDEN,
                                      status=status, created=self.clock()))
                    return None
                title, final = job_title(job, self.redact)
                naming = not final and self.namer is not None
                report = self._save(Report(job_id=job.id, scope=job.scope, destination=destination, title=title,
                                           naming=naming, name_input=name_source(job, self.redact) if naming else "",
                                           status=status, created=self.clock()))
            else:
                if report.renaming:  # newer admitted work, or a record stored before its input was
                    source = name_source(job, self.redact)
                    if source != report.name_input:
                        report = self._save(replace(report, name_input=source))
                if report.holding or self.passed.get(job.id):
                    passed = self.passed.setdefault(job.id, [])
                    if status != (passed[-1] if passed else report.status):
                        passed.append(status)
                    if report.holding:
                        return report
                elif not report.status or report.status == status:
                    return report
                else:
                    report = self._show(report, status)
        self.dispatch(job.id)
        return report

    def _show(self, report, status):
        return self._save(replace(report, status=status, attempts=0 if report.state == SENT else report.attempts,
                                  not_before=0.0 if report.state == SENT else report.not_before))

    def _replay(self, job_id):
        """After naming, the statuses a job passed meanwhile, one per edit, once its message exists."""
        report, passed = self.reports.get(job_id), self.passed.get(job_id)
        if report is None or report.state == PENDING or not passed:
            return None
        self._show(report, passed.pop(0))
        if not passed:
            del self.passed[job_id]
        return self._claim(job_id) or self._replay(job_id)

    def dispatch(self, job_id):
        threading.Thread(target=self.drive, args=(job_id,), daemon=True).start()

    def drive(self, job_id):
        """Bring the message to the latest status, one Telegram call at a time; a second caller returns at
        once and the running one picks up whatever changed meanwhile."""
        with self.lock:
            if job_id in self.busy:
                return self.reports.get(job_id)
            self.busy.add(job_id)
        try:
            while True:
                with self.lock:
                    step = self._claim(job_id) or self._replay(job_id)
                    if step is None:
                        self.busy.discard(job_id)
                        return self.reports.get(job_id)
                kind, status, report = step
                if kind == "name":
                    title, why = self._name(report)
                    with self.lock:
                        self._named(job_id, title, why)
                    continue
                destination = Destination(**report.destination)
                try:
                    result = (self.sender.send(destination, report.text) if kind == "send" else
                              self.sender.edit(destination, report.message_id, report.text))
                except Exception as exc:
                    result = {"error": f"sender raised {type(exc).__name__}; it may have been delivered"}
                with self.lock:
                    (self._sent if kind == "send" else self._edited)(job_id, status, result)
        except BaseException:
            with self.lock:
                self.busy.discard(job_id)
            raise

    def _claim(self, job_id):
        """The next step: the first naming attempt, then the send or a due edit, then a due naming retry."""
        report = self.reports.get(job_id)
        if report is None or not report.status:
            return None
        if report.holding:
            return "name", report.status, report
        now = self.clock()
        if report.state == PENDING or report.needs_edit:
            wait = report.not_before - now
            if wait > 0:
                self._later(wait, job_id)
                return None
            if report.state == PENDING:
                return "send", report.status, self._save(report.to(SENDING, attempts=report.attempts + 1,
                                                                    owner=self.owner))
            return "edit", report.status, self._save(replace(report, attempts=report.attempts + 1))
        if report.renaming:
            if report.name_after <= now:
                return "name", report.status, report
            self._later(report.name_after - now, job_id)
        return None

    def _name(self, report):
        """(the host model's usable English title or "", why not); asked outside the lock."""
        if self.namer is None:
            return "", "no model"
        try:
            named = self.namer(report.name_input)
        except Exception as exc:  # no route bound yet, an outage or a refusal: retried with backoff
            reason = str(exc) if type(exc).__name__ == "ModelUnavailable" else type(exc).__name__
            return "", reason[:100]
        title = usable_title(self.redact(named if isinstance(named, str) else ""))
        return title, "" if title else "the answer was not a short plain English sentence"

    def _named(self, job_id, title, why):
        """Apply one naming attempt (the lock is held). A title named after the send renames the message by an edit."""
        report = self.reports[job_id]
        tries = report.name_tries + 1
        if title:
            renamed = report.state == SENT and title != report.title
            self._save(replace(report, title=title, naming=False, name_input="", name_tries=tries,
                               shown=None if renamed else report.shown,
                               attempts=0 if renamed else report.attempts,
                               not_before=0.0 if renamed else report.not_before))
            logger.info("task status title named (attempt %d)", tries)
        elif tries >= TITLE_ATTEMPTS:
            kept = "the task number" if report.title.startswith("Hermes task ") else "the admitted T3 title"
            self._save(replace(report, naming=False, name_input="", name_tries=tries,
                               notes=report.notes + (f"title not named: {why}",)))
            logger.warning("task status title not named after %d attempts (%s); it keeps %s", tries, why, kept)
        else:
            wait = self.name_backoff[min(tries - 1, len(self.name_backoff) - 1)]
            self._save(replace(report, name_tries=tries, name_after=self.clock() + wait))
            logger.info("task status title not named yet (attempt %d of %d: %s)", tries, TITLE_ATTEMPTS, why)

    def _retry_at(self, attempts, error=""):
        wait = self.backoff[min(attempts - 1, len(self.backoff) - 1)]
        return self.clock() + max(wait, retry_after(error) or 0.0)

    def _sent(self, job_id, status, result):
        report = self.reports[job_id]
        state, message_id, error = classify(result)
        error = self.redact(error)
        if state == PENDING and report.attempts >= self.attempts:
            state, error = FAILED, f"{error} (gave up after {report.attempts} attempts)"
        if state == PENDING:
            self._save(report.to(PENDING, error=error, owner=None, not_before=self._retry_at(report.attempts, error),
                                 notes=report.notes + (f"not sent: retry {report.attempts}",)))
        elif state == SENT:
            self._save(report.to(SENT, message_id=message_id, shown=status, attempts=0, error=error, owner=None))
        else:
            self._save(report.to(state, error=error, owner=None))

    def _edited(self, job_id, status, result):
        report = self.reports[job_id]
        outcome = classify_edit(result)
        error = self.redact(str(result.get("error") if isinstance(result, dict) else result))[:300]
        if outcome == EDIT_DONE:
            self._save(replace(report, shown=status, attempts=0, error="", not_before=0.0, stalled=False))
        elif outcome == EDIT_RETRY and report.attempts < self.attempts:
            self._save(replace(report, error=error, not_before=self._retry_at(report.attempts, error)))
        else:
            self._save(replace(report, held=status, stalled=outcome == EDIT_RETRY, attempts=0, error=error,
                               not_before=0.0, notes=report.notes + (f"edit to {status} not applied",)))

    def _later(self, delay, job_id):
        timer = threading.Timer(delay, self.drive, args=(job_id,))
        timer.daemon = True
        timer.start()

    def reconcile(self):
        """After a restart: a claim this process does not own may have reached Telegram; never resend it.
        Unsent and lagging messages are brought up to date."""
        with self.lock:
            for report in list(self.reports.values()):
                if report.state == SENDING and report.owner != self.owner:
                    self._save(report.to(UNCERTAIN, owner=None, error="Talk stopped during the send; it may or "
                                         "may not have arrived and was not resent.",
                                         notes=report.notes + ("claim found after restart",)))
                elif report.stalled or report.held and any(m in (report.error or "") for m in OUTAGE_HOLDS):
                    self._save(replace(report, held=None, stalled=False, attempts=0, not_before=0.0))
            due = [r.job_id for r in self.reports.values()
                   if r.status and (r.state == PENDING or r.needs_edit or r.renaming)]
        for job_id in due:
            self.dispatch(job_id)

    def wait_settled(self, job_id, timeout):
        """The report once nothing is left to do for it (or at the timeout)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                report = self.reports.get(job_id)
                idle = job_id not in self.busy
            if report is not None and idle and report.state in (SENT, FAILED, UNCERTAIN, HIDDEN) \
                    and not report.needs_edit and not report.renaming:
                return report
            time.sleep(0.02)
        return self.reports.get(job_id)
