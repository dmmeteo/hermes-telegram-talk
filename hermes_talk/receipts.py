"""One live Telegram status message per Hermes task, sent once and then edited through Hermes' own sender.

The message is a status mark and a few English words naming the task: queued, running, completed, failed
or cancelled. It never carries the result, which stays in the native task session and reaches the user by
voice. The title is the voice model's TITLE line when it gave a usable one; otherwise the request itself,
shortened the way Conduit titles a background job (VoiceBackgroundJobs.title(for:)), when it is English;
otherwise one short English sentence from the profile's own model through the plugin's host ctx.llm, asked
once before the only send. If that gives nothing usable, the request's own words stay the title.

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
import asyncio
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, fields, replace

from . import jobs
from .agent import process_start

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
NOT_SENT_RETRYABLE = ("connecterror", "connection refused", "too many requests", "retry after", "flood control")
NOT_SENT_FINAL = ("chat not found", "bot was blocked", "forbidden", "user is deactivated", "bad request",
                  "can't initiate conversation", "not installed", "not configured", "no deliverable",
                  "unknown platform", "message is too long")
EDIT_DONE, EDIT_RETRY, EDIT_FINAL = "done", "retry", "final"
EDIT_REFUSED = ("message to edit not found", "message can't be edited", "chat not found", "bot was blocked",
                "forbidden", "user is deactivated", "bad request", "not installed", "not configured")

TITLE_LINE = re.compile(r"TITLE\s*:\s*(.*)")
TITLE_CHARS = 60  # Conduit VoiceBackgroundJobs.maximumTitleCharacters
TITLE_WORDS = 8
TITLE_PURPOSE = "talk_task_title"
TITLE_TIMEOUT_S = 20.0
TITLE_INSTRUCTIONS = (
    "You name a background task for its one-line status message. The request below is what the user asked an "
    "assistant to do, possibly in another language. Reply with a JSON object {\"title\": \"...\"}: one short plain "
    f"English sentence of at most {TITLE_WORDS} words saying what the task does. No quotes, markup, emoji or numbers.")
# Markup that Hermes' formatter (first send) and a plain-text edit would render differently.
MARKUP = re.compile(r"[*_`\[\]~|<>#\\]")
DIRECTIVE = re.compile(r"(?im)^\s*report\s+to\s*:\s*(.+?)\s*$")
RETRY_AFTER = re.compile(r"(?i)retry (?:in|after) (\d+(?:\.\d+)?)")


class TransitionError(Exception):
    pass


def conduit_title(text):
    """Conduit's VoiceBackgroundJobs.title(for:): whitespace collapsed; past 60 characters, cut at the last
    space beyond the midpoint (else hard) and ended with an ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= TITLE_CHARS:
        return collapsed
    cut = collapsed[:TITLE_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > TITLE_CHARS // 2 else cut) + "…"


def _plain(text):
    return " ".join(MARKUP.sub(" ", text).split())


def _english(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isascii() for c in letters)


def usable_title(text):
    """A plain English title of a few words, Conduit-shortened, else ""."""
    title = _plain(text)
    return conduit_title(title) if _english(title) and len(title.split()) <= TITLE_WORDS else ""


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
    """(title, final). A request in another language is shown in its own words only if the host model cannot name it."""
    if job.title:
        return conduit_title(_plain(redact(job.title))), True
    request = _plain(redact(job.request))
    return conduit_title(request), _english(request)


def ask_title(model, request):
    """The host model's English title for a request (pulse_model.PulseModel over ctx.llm); raises when it has none."""
    title = model.ask(TITLE_INSTRUCTIONS, request, TITLE_PURPOSE, max_tokens=1000, timeout=TITLE_TIMEOUT_S).get("title")
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
    `stalled` marks a give-up on temporary errors, which a restart retries; `naming` a title still in the request's
    own words, which the host model is asked to name once before the first send. While it names, `status` stays the
    first one and later statuses wait in order, so every step is still shown."""
    job_id: str
    scope: str
    destination: dict
    title: str = ""
    status: str = ""  # "" only for a report stored by an earlier version, which is kept as is
    shown: str = None
    held: str = None
    stalled: bool = False
    naming: bool = False
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
    """(state, message id, error) of a first send, from Hermes' sender result dict."""
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


class TelegramSender:
    """Hermes' native Telegram calls with this Talk server's bot token, which Talk already holds and never
    stores: the platform sender cron delivery uses (tools.send_message_tool._send_to_platform) to create the
    message, and the same module's Bot factory (tools.send_message_senders._telegram_bot) to edit it."""

    def __init__(self, bot_token, timeout=SEND_TIMEOUT_S):
        self.token = bot_token
        self.timeout = timeout

    def _foreign(self, destination):
        if destination.bot_id and destination.bot_id != self.token.split(":", 1)[0]:
            return {"error": "not configured: the report is bound to another bot than this server's"}
        return None

    def send(self, destination, text):
        refused = self._foreign(destination)
        if refused:
            return refused
        try:
            from gateway.config import Platform, PlatformConfig
            from tools.send_message_tool import _send_to_platform
        except Exception as exc:
            return {"error": f"Hermes platform sender is not installed ({type(exc).__name__})"}
        thread = str(destination.thread_id) if destination.thread_id is not None else None

        async def run():
            return await asyncio.wait_for(_send_to_platform(
                Platform.TELEGRAM, PlatformConfig(enabled=True, token=self.token), str(destination.chat_id), text,
                thread_id=thread), timeout=self.timeout)
        try:
            return asyncio.run(run())
        except TimeoutError:
            return {"error": f"send timed out after {self.timeout:.0f}s; it may still have been delivered"}
        except Exception as exc:
            return {"error": f"send raised {type(exc).__name__}; it may have been delivered"}

    def edit(self, destination, message_id, text):
        refused = self._foreign(destination)
        if refused:
            return refused
        try:
            from tools.send_message_senders import _telegram_bot
        except Exception as exc:
            return {"error": f"Hermes Telegram sender is not installed ({type(exc).__name__})"}

        async def run():
            await asyncio.wait_for(_telegram_bot(self.token).edit_message_text(
                chat_id=destination.chat_id, message_id=int(message_id), text=text), timeout=self.timeout)
            return {"success": True, "message_id": str(message_id)}
        try:
            return asyncio.run(run())
        except TimeoutError:
            return {"error": f"edit timed out after {self.timeout:.0f}s"}
        except Exception as exc:
            return {"error": f"Telegram edit failed: {exc}"}


class ReportDesk:
    """Durable status messages: created once at a task's first status, claimed before the only send, then
    edited to each later status by one worker per task."""

    def __init__(self, history, sender, clock, attempts=3, backoff=(5.0, 30.0), redact=str, namer=None, visible=None):
        self.history, self.sender, self.clock, self.redact, self.namer = history, sender, clock, redact, namer
        self.visible = visible  # job -> whether a job without a message gets one (notify.py); None: every job
        self.requests = {}  # job id -> redacted request, for a report still naming
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
                report = self._save(Report(job_id=job.id, scope=job.scope, destination=destination, title=title,
                                           naming=not final and self.namer is not None, status=status,
                                           created=self.clock()))
            elif report.naming or self.passed.get(job.id):
                self._remember(report, job)
                passed = self.passed.setdefault(job.id, [])
                if status != (passed[-1] if passed else report.status):
                    passed.append(status)
                if report.naming:
                    return report
            elif not report.status or report.status == status:
                return report
            else:
                report = self._show(report, status)
            self._remember(report, job)
        self.dispatch(job.id)
        return report

    def _remember(self, report, job):
        if report.naming:
            self.requests[job.id] = _plain(self.redact(job.request))

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
                    title = self._name(report)
                    with self.lock:
                        self._save(replace(self.reports[job_id], title=title, naming=False))
                        self.requests.pop(job_id, None)
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
        report = self.reports.get(job_id)
        if report is None or not report.status or not (report.state == PENDING or report.needs_edit):
            return None
        if report.state == PENDING and report.naming:
            return "name", report.status, report
        wait = report.not_before - self.clock()
        if wait > 0:
            self._later(wait, job_id)
            return None
        if report.state == PENDING:
            return "send", report.status, self._save(report.to(SENDING, attempts=report.attempts + 1,
                                                                owner=self.owner))
        return "edit", report.status, self._save(replace(report, attempts=report.attempts + 1))

    def _name(self, report):
        """The host model's English title, else the request's own words; asked outside the lock."""
        try:
            named = self.namer(self.requests.get(report.job_id, report.title)) if self.namer else ""
        except Exception:  # no model route, an outage or a refusal: the request's own words name the task
            named = ""
        return usable_title(self.redact(named or "")) or report.title

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
                elif report.stalled:
                    self._save(replace(report, held=None, stalled=False, attempts=0, not_before=0.0))
            due = [r.job_id for r in self.reports.values() if r.status and (r.state == PENDING or r.needs_edit)]
        for job_id in due:
            self.dispatch(job_id)

    def wait_settled(self, job_id, timeout):
        """The report once nothing is left to do for it (or at the timeout)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                report = self.reports.get(job_id)
                idle = job_id not in self.busy
            if report is not None and idle and report.state in (SENT, FAILED, UNCERTAIN, HIDDEN) and not report.needs_edit:
                return report
            time.sleep(0.02)
        return self.reports.get(job_id)
