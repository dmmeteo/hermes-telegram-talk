"""Confirmed T3 work of a voice task: what the core's post_tool_call hook saw, and how Talk follows it.

A task is large only when the server observes T3 admit work started from it: a successful `t3_control` start or
send in the task's own native turn. Reads, lists, waits, refusals and other threads never count. The hook runs in
whichever process runs the turn (the `hermes chat` child, which carries HERMES_TALK_JOB, or the profile gateway,
which finds the job by the session marker Talk wrote before submitting) and writes one observation file per
admission, keyed by the call's request_id, so a retried call never makes a second link.

Talk then follows each link through the profile's own `t3_control` handler (registry dispatch, which fires no tool
hooks, so these reads never look like admissions). A link is done only when T3 shows its run settled; an unreadable
T3 changes nothing. A possibly admitted start or send (T3's answer was lost) is confirmed only by the thread itself
and is never resent.
"""
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

ENV_JOB = "HERMES_TALK_JOB"
WORK_DIR = ("plugin-data", "hermes-talk", "work")
JOB_ID = re.compile(r"jb_[A-Za-z0-9_-]{16,64}")
ADMITTING = ("start", "send")
LIVE = ("running", "blocked")
DONE = frozenset({"completed", "failed", "interrupted", "idle"})
DROPPED = "unconfirmed"
RESULT_CHARS = 6000


@dataclass(frozen=True)
class Link:
    """One T3 run a task started (`start`) or continued (`send`), and what T3 last showed of it.

    `base_runs` is the thread's run count before this run (None when unknown); `queued` marks a send T3 put behind
    another active run; `started` that this run was seen live or counted. `confirmed` is False only for an admission
    whose answer was lost, until the thread shows the run."""
    key: str
    thread_id: str
    via: str
    confirmed: bool
    base_runs: int = None
    queued: bool = False
    started: bool = False
    status: str = "queued"
    result: str = ""
    error: str = ""
    continues: int = None
    admitted: float = 0.0
    seen: float = 0.0  # when T3 first confirmed or disproved a possible admission

    @property
    def done(self):
        return self.status in DONE or self.status == DROPPED

    @property
    def counts(self):
        return self.confirmed and self.status != DROPPED


LINK_FIELDS = {f.name for f in fields(Link)}


def link_dump(link):
    return asdict(link)


def link_load(data):
    try:
        return Link(**{k: v for k, v in data.items() if k in LINK_FIELDS})
    except (TypeError, AttributeError):
        return None


def work_dir(home):
    return Path(home).joinpath(*WORK_DIR)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mark_session(home, session_id, job_id):
    """Which job a gateway session runs, for the hook in the gateway process (CLI children carry HERMES_TALK_JOB)."""
    _write(work_dir(home) / f"session-{_digest(session_id)}.json", {"session_id": session_id, "job_id": job_id})


def unmark_session(home, session_id, job_id):
    """Once the job's turn ended; a later task that resumed the same session keeps its own marker."""
    path = work_dir(home) / f"session-{_digest(session_id)}.json"
    if (_read(path) or {}).get("job_id") == job_id:
        try:
            path.unlink()
        except OSError:
            pass


def _session_job(home, session_id):
    if not session_id:
        return None
    data = _read(work_dir(home) / f"session-{_digest(str(session_id))}.json")
    return data.get("job_id") if data and data.get("session_id") == str(session_id) else None


def _home():
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME")


def _result(result):
    if isinstance(result, dict):
        return result
    try:
        data = json.loads(result) if isinstance(result, str) else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def observe(tool_name=None, args=None, result=None, session_id=None, home=None, environ=None, clock=time.time, **_):
    """`post_tool_call` body: record a T3 start or send this Talk task's turn made; returns the record or None.

    Only an `ok` answer, or a lost answer T3 may have committed (`outcome_unknown` with a thread id), is recorded.
    A refusal, a status or read call, or a call outside a Talk task records nothing."""
    if tool_name != "t3_control" or not isinstance(args, dict) or args.get("action") not in ADMITTING:
        return None
    environ = os.environ if environ is None else environ
    home = home or _home()
    if not home:
        return None
    job_id = environ.get(ENV_JOB) or _session_job(home, session_id)
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        return None
    data = _result(result)
    if data is None:
        return None
    ok = data.get("ok") is True
    if not ok and data.get("outcome_unknown") is not True:
        return None
    thread_id = data.get("thread_id") or (args.get("thread_id") if args["action"] == "send" else None)
    if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 600:
        return None
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    run = state.get("run") if isinstance(state.get("run"), dict) else {}
    count = run.get("count")
    record = {"job_id": job_id, "session_id": str(session_id or ""), "action": args["action"], "thread_id": thread_id,
              "confirmed": ok, "delivery": data.get("delivery") if args["action"] == "send" else "started",
              "outcome": state.get("outcome") if ok else None,
              "run_count": count if type(count) is int and ok else None, "at": clock()}
    record["key"] = _digest(f"{args['action']}:{args.get('request_id')}:{thread_id}")
    path = work_dir(home) / job_id / f"{record['key']}.json"
    earlier = _read(path)
    if earlier is not None and earlier.get("confirmed") and not ok:
        return earlier  # a later lost answer of the same request never downgrades what T3 confirmed
    _write(path, record)
    return record


def observations(home, job_id):
    """The admissions recorded for a job, oldest first."""
    folder = work_dir(home) / job_id
    if not JOB_ID.fullmatch(job_id) or not folder.is_dir():
        return []
    found = [_read(p) for p in folder.glob("*.json")]
    return sorted((r for r in found if r and r.get("job_id") == job_id and isinstance(r.get("key"), str)),
                  key=lambda r: (r.get("at") or 0, r["key"]))


def link_from(record):
    """The link an observation opens. A start's thread is new, so its run is the first one."""
    via = record.get("action")
    confirmed = record.get("confirmed") is True
    queued = via == "send" and record.get("delivery") == "queued"
    count = record.get("run_count") if type(record.get("run_count")) is int else None
    base = 0 if via == "start" else count
    live = record.get("outcome") in LIVE
    # A confirmed send without a readable count: the run was admitted, so whatever T3 shows next is about it.
    started = confirmed and ((live and not queued) or (via == "send" and base is None and not queued))
    return Link(key=record["key"], thread_id=record["thread_id"], via=via, confirmed=confirmed, base_runs=base,
                queued=queued, started=started, status="running" if started and live else "queued",
                admitted=float(record.get("at") or 0))


def merge(link, record):
    """A repeated observation of the same request: only a confirmation changes the link."""
    if link.confirmed or record.get("confirmed") is not True:
        return link
    fresh = link_from(record)
    return replace(fresh, status=link.status if link.started else fresh.status, continues=link.continues)


def _final_text(messages):
    for message in reversed(messages or ()):
        if isinstance(message, dict) and message.get("role") == "assistant" and (message.get("text") or "").strip():
            return message["text"].strip()
    return ""


def assess(link, read, now):
    """The link after one T3 read. Nothing it cannot read is ever concluded."""
    if link.done or not isinstance(read, dict):
        return link
    if read.get("ok") is not True:
        if read.get("error") == "not_found" and not link.confirmed:
            return replace(link, status=DROPPED, seen=now, error="T3 has no such thread; nothing was admitted.")
        return link
    state = read.get("state") if isinstance(read.get("state"), dict) else {}
    outcome = state.get("outcome")
    run = state.get("run") if isinstance(state.get("run"), dict) else {}
    count = run.get("count") if type(run.get("count")) is int else None
    counted = count is not None and link.base_runs is not None and count > link.base_runs
    live = outcome in LIVE
    if not link.confirmed:
        # A start's thread id is derived from its request, so the thread exists only if T3 committed the launch.
        if link.via == "start" or counted or (live and link.base_runs is None):
            link = replace(link, confirmed=True, seen=now)
        else:
            return replace(link, status=DROPPED, seen=now,
                           error="T3 never showed a run for the possibly sent message; it was not resent.")
    started = link.started or counted or (live and (not link.queued or link.base_runs is None))
    link = replace(link, started=started)
    if not started:
        return replace(link, status="queued")
    if count is not None and link.base_runs is not None and count > link.base_runs + 1:
        # A later run began, which T3 starts only after this one ended; how it ended is no longer shown.
        text = _final_text(read.get("messages"))[-RESULT_CHARS:]
        return replace(link, status="completed",
                       result=text or "Its T3 run ended and a later message continued the thread.")
    if outcome == "blocked":
        return replace(link, status="blocked")
    if live:
        return replace(link, status="running")
    if outcome in DONE:
        text = _final_text(read.get("messages"))[-RESULT_CHARS:]
        return replace(link, status=outcome, result=text if outcome in ("completed", "idle") else "",
                       error=text[-300:] if outcome == "failed" else "")
    return link


def work_status(links):
    """What a task's T3 work shows now: blocked beats running beats queued; None once nothing is open."""
    open_ = [l.status for l in links if l.counts and not l.done]
    for status in ("blocked", "running", "queued"):
        if status in open_:
            return status
    return None


class T3:
    """The profile's own t3_control handler, read and interrupted from the Talk server (no tool hooks fire)."""

    def __init__(self, dispatch=None):
        self._dispatch = dispatch

    def call(self, args):
        try:
            if self._dispatch is not None:
                raw = self._dispatch(args)
            else:
                from tools.registry import registry
                raw = registry.dispatch("t3_control", args)
        except Exception as exc:
            return {"ok": False, "error": "unavailable", "message": type(exc).__name__}
        data = _result(raw)
        return data if data is not None else {"ok": False, "error": "unreadable"}

    def read(self, thread_id):
        return self.call({"action": "read", "thread_id": thread_id, "messages": 2, "max_chars": RESULT_CHARS})

    def interrupt(self, thread_id):
        return self.call({"action": "interrupt", "thread_id": thread_id})
