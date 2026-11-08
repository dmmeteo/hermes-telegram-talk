"""Short final task reports in Telegram, sent once per job through Hermes' own platform sender.

A report is created when its job reaches a terminal state and is stored, with its bound
destination, in the profile's state.db (SessionDB meta) before anything is sent. Sending first
claims it (pending -> sending, persisted), so a crash or restart never sends it blindly again:
a claim left by an earlier process becomes "uncertain". Telegram's sendMessage has no client
idempotency key, so a timed-out or otherwise ambiguous send is also "uncertain" and never resent.
Only failures that prove nothing was sent (refused connection, rate limit) go back to pending,
at most `attempts` times. Execution state (jobs.py), voice receipt and chat report stay separate.

The default destination is the private chat between the authenticated user and the bot whose
signed initData opened the call. A configured named destination replaces it when the user asked
for it explicitly; nothing is duplicated to the private chat then.
"""
import asyncio
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .agent import process_start
from .store import clip

PENDING, SENDING, SENT, FAILED, UNCERTAIN = "pending", "sending", "sent", "failed", "uncertain"
TRANSITIONS = {PENDING: {SENDING}, SENDING: {SENT, PENDING, FAILED, UNCERTAIN}}
REPORT_PREFIX = "hermes_talk:report:"
SINCE_KEY = "hermes_talk:reports_since"
SEND_TIMEOUT_S = 30.0
COVER_CHARS = 300
NOT_SENT_RETRYABLE = ("connecterror", "connection refused", "too many requests", "retry after", "flood control")
NOT_SENT_FINAL = ("chat not found", "bot was blocked", "forbidden", "user is deactivated", "bad request",
                  "can't initiate conversation", "not installed", "not configured", "no deliverable",
                  "unknown platform", "message is too long")
HEADS = {"succeeded": "✅", "failed": "⚠️", "cancelled": "⏹", "interrupted": "⚠️"}


class TransitionError(Exception):
    pass


@dataclass(frozen=True)
class Destination:
    """Where a job's report goes; bound from authenticated server state when the job is accepted."""
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
    job_id: str
    scope: str
    destination: dict
    text: str
    attachment: str = None
    state: str = PENDING
    attempts: int = 0
    not_before: float = 0.0
    message_id: str = None
    error: str = ""
    owner: str = None
    created: float = 0.0
    updated: float = 0.0
    notes: tuple = field(default_factory=tuple)

    def to(self, state, **fields):
        if state not in TRANSITIONS.get(self.state, ()):
            raise TransitionError(f"{self.state} -> {state}")
        return replace(self, state=state, **fields)

    def public(self):
        return {"state": self.state, "destination": Destination(**self.destination).label()}


def report_key(job_id):
    return REPORT_PREFIX + job_id


def load(raw):
    try:
        data = json.loads(raw) if raw else None
        data["notes"] = tuple(data.get("notes") or ())
        return Report(**data)
    except (ValueError, TypeError, AttributeError):
        return None


def render(job, full, limit, redact=str):
    """(text, full result for an attachment or None): what happened, the result or reason, nothing else."""
    head = f"{HEADS.get(job.state, '•')} Hermes task {job.number} {job.spoken_state()}: {clip(redact(job.request), 120)}"
    body = redact(full or "").strip()
    if len(head) + 2 + len(body) <= limit:
        return (head + "\n\n" + body).strip(), None
    cover = clip(body, min(COVER_CHARS, max(40, limit - len(head) - 60)))
    return f"{head}\n\n{cover}\n\nFull result attached ({len(body)} characters).", body


def classify(result):
    """(state, message id, error) from Hermes' sender result dict."""
    if isinstance(result, dict) and result.get("success"):
        return SENT, str(result.get("message_id") or ""), "; ".join(result.get("warnings") or [])[:300]
    error = str((result or {}).get("error") if isinstance(result, dict) else result or "no result")[:300]
    lowered = error.lower()
    if isinstance(result, dict) and result.get("warnings"):
        # Part of the send (an attachment upload) failed in a way that may still have reached Telegram.
        return UNCERTAIN, None, (error + "; " + "; ".join(map(str, result["warnings"])))[:300]
    if any(m in lowered for m in NOT_SENT_FINAL):
        return FAILED, None, error
    if any(m in lowered for m in NOT_SENT_RETRYABLE):
        return PENDING, None, error
    return UNCERTAIN, None, error


class TelegramSender:
    """Hermes' native platform sender (tools.send_message_tool._send_to_platform, the call cron delivery
    makes) with this Talk server's bot token, which Talk already holds and never stores."""

    def __init__(self, bot_token, timeout=SEND_TIMEOUT_S):
        self.token = bot_token
        self.timeout = timeout

    def send(self, destination, text, attachment):
        if destination.bot_id and destination.bot_id != self.token.split(":", 1)[0]:
            return {"error": "not configured: the report is bound to another bot than this server's"}
        try:
            from gateway.config import Platform, PlatformConfig
            from tools.send_message_tool import _send_to_platform
        except Exception as exc:
            return {"error": f"Hermes platform sender is not installed ({type(exc).__name__})"}
        media = [(attachment, False)] if attachment else []
        thread = str(destination.thread_id) if destination.thread_id is not None else None

        async def run():
            return await asyncio.wait_for(_send_to_platform(
                Platform.TELEGRAM, PlatformConfig(enabled=True, token=self.token), str(destination.chat_id), text,
                thread_id=thread, media_files=media, force_document=True), timeout=self.timeout)
        try:
            return asyncio.run(run())
        except TimeoutError:
            return {"error": f"send timed out after {self.timeout:.0f}s; it may still have been delivered"}
        except Exception as exc:
            return {"error": f"send raised {type(exc).__name__}; it may have been delivered"}


class ReportDesk:
    """Durable per-job reports: create once, claim before sending, never resend an ambiguous send."""

    def __init__(self, history, sender, clock, home, limit=700, attempts=3, backoff=(5.0, 30.0), redact=str):
        self.history, self.sender, self.clock, self.redact = history, sender, clock, redact
        self.limit, self.attempts, self.backoff = limit, attempts, backoff
        self.dir = Path(home).joinpath("plugin-data", "hermes-talk", "reports")
        self.owner = f"{os.getpid()}:{process_start(os.getpid())}:{secrets.token_hex(4)}"
        self.lock = threading.RLock()
        # Jobs that finished before reports existed in this profile never get one.
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
        return self.reports.get(job_id)

    def prepare(self, job, full):
        """The job's report, created and persisted on its first terminal transition only."""
        with self.lock:
            if job.id in self.reports:
                return self.reports[job.id]
            text, full = render(job, full, self.limit, self.redact)
            attachment = None
            if full is not None:
                self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.dir / f"hermes-task-{job.number}-{job.id[-8:]}.md"
                path.write_text(f"# Hermes task {job.number}\n\nRequest: {self.redact(job.request)}\n\n{full}\n",
                                encoding="utf-8", errors="replace")
                attachment = str(path)
            destination = job.report_to or asdict(Destination.private(int(job.scope.rsplit(":", 1)[1]),
                                                                      job.scope.rsplit(":", 2)[1][2:]))
            return self._save(Report(job_id=job.id, scope=job.scope, destination=destination, text=text,
                                     attachment=attachment, created=self.clock()))

    def dispatch(self, job_id):
        threading.Thread(target=self.send, args=(job_id,), daemon=True).start()

    def send(self, job_id):
        with self.lock:
            report = self.reports.get(job_id)
            if report is None or report.state != PENDING:
                return report
            wait = report.not_before - self.clock()
            if wait > 0:
                self._later(wait, job_id)
                return report
            report = self._save(report.to(SENDING, attempts=report.attempts + 1, owner=self.owner))
        try:
            result = self.sender.send(Destination(**report.destination), report.text, report.attachment)
        except Exception as exc:
            result = {"error": f"sender raised {type(exc).__name__}; it may have been delivered"}
        state, message_id, error = classify(result)
        with self.lock:
            if state == PENDING and report.attempts >= self.attempts:
                state, error = FAILED, f"{error} (gave up after {report.attempts} attempts)"
            if state == PENDING:
                delay = self.backoff[min(report.attempts - 1, len(self.backoff) - 1)]
                report = self._save(report.to(PENDING, error=error, owner=None, not_before=self.clock() + delay,
                                              notes=report.notes + (f"not sent: retry {report.attempts}",)))
                self._later(delay, job_id)
                return report
            return self._save(report.to(state, message_id=message_id, error=error, owner=None))

    def _later(self, delay, job_id):
        timer = threading.Timer(delay, self.send, args=(job_id,))
        timer.daemon = True
        timer.start()

    def reconcile(self):
        """After a restart: a claim this process does not own may have reached Telegram; never resend it."""
        with self.lock:
            for report in list(self.reports.values()):
                if report.state == SENDING and report.owner != self.owner:
                    self._save(report.to(UNCERTAIN, owner=None, error="Talk stopped during the send; it may or "
                                         "may not have arrived and was not resent.",
                                         notes=report.notes + ("claim found after restart",)))
            pending = [r.job_id for r in self.reports.values() if r.state == PENDING]
        for job_id in pending:
            self.dispatch(job_id)

    def wait_settled(self, job_id, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            report = self.reports.get(job_id)
            if report is not None and report.state in (SENT, FAILED, UNCERTAIN):
                return report
            time.sleep(0.05)
        return self.reports.get(job_id)


DIRECTIVE = re.compile(r"(?im)^\s*report\s+to\s*:\s*(.+?)\s*$")
