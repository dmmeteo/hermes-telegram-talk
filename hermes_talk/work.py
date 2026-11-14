"""Confirmed T3 work of a voice task: what the core's post_tool_call hook saw, and how Talk follows it.

A task is large only when the server observes T3 admit work started from it: a successful `t3_control` start or
send in the task's own native turn. Reads, lists, waits, refusals and other threads never count. The hook runs in
whichever process runs the turn (the `hermes chat` child, which carries HERMES_TALK_JOB, or the profile gateway,
which finds the job by the session marker Talk wrote before submitting) and writes one observation file per
admission, keyed by the call's request_id, so a retried call never makes a second link.

A gateway turn whose context was compressed continues in a child session; the hook follows the session's
parent chain (public SessionDB.get_session) back to the marked one.

Talk then follows each link through the profile's own `t3_control` handler (registry dispatch, which fires no tool
hooks, so these reads never look like admissions). A link is its own T3 run: identified by the run id T3 shows
(`active_id`, else the newest `latest_id` that is not the run it was admitted behind), with the thread's run count as
the fallback when T3 shows no ids. Its outcome is read only while it is the thread's latest run: completed, failed or
interrupted, with a result only when the thread's newest message is the assistant's. T3's read is per thread, not per
run, so when a later run started before Talk saw this one settle, or the run cannot be singled out, or T3 shows only
`idle`, the outcome is `unknown`: never success, and never the later run's text. An unreadable T3 changes nothing. A possibly admitted
start or send (T3's answer was lost) is confirmed only by the thread itself and is never resent; a lost start that
came back without a thread id is kept as a note, since nothing can follow it.
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
LINEAGE_HOPS = 16
SETTLED = frozenset({"completed", "failed", "interrupted", "idle"})  # thread outcomes of a run that is not live
UNKNOWN = "unknown"  # the run ended, but T3 no longer shows how: never reported as success or with another run's text
DONE = frozenset({"completed", "failed", "interrupted", UNKNOWN})
DROPPED = "unconfirmed"
LOST_START = ("T3's answer to a start was lost before it named a thread; T3 may run that work unseen by Talk. "
              "Check T3.")
RESULT_CHARS = 6000


@dataclass(frozen=True)
class Link:
    """One T3 run a task started (`start`) or continued (`send`), and what T3 last showed of it.

    `run_id` is this run's T3 id once known; `prior_run_id` the run it must differ from (the active run a queued send
    waits behind, or the latest run of an idle thread); `base_runs` the thread's run count before it (None when
    unknown), for a T3 that shows no run ids. `started` marks the run seen live or counted. `confirmed` is False only
    for an admission whose answer was lost, until the thread shows the run."""
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
    run_id: str = None
    prior_run_id: str = None

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


def _marked(home, session_id):
    data = _read(work_dir(home) / f"session-{_digest(str(session_id))}.json")
    return data.get("job_id") if data and data.get("session_id") == str(session_id) else None


def _parent(home, session_id):
    """The session a compressed one continues, through the public SessionDB (None when there is none)."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=Path(home) / "state.db")
    try:
        row = db.get_session(session_id) or {}
    finally:
        db.close()
    return row.get("parent_session_id")


def _session_job(home, session_id, parent=_parent):
    """The job whose turn runs in this session, or in the session it was compressed from."""
    seen = set()
    while session_id and str(session_id) not in seen and len(seen) < LINEAGE_HOPS:
        seen.add(str(session_id))
        job_id = _marked(home, session_id)
        if job_id:
            return job_id
        try:
            session_id = parent(home, str(session_id))
        except Exception:
            return None
    return None


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


def observe(tool_name=None, args=None, result=None, session_id=None, home=None, environ=None, clock=time.time,
            parent=_parent, **_):
    """`post_tool_call` body: record a T3 start or send this Talk task's turn made; returns the record or None.

    Only an `ok` answer, or a lost answer T3 may have committed (`outcome_unknown`), is recorded. A refusal, a status
    or read call, or a call outside a Talk task records nothing."""
    if tool_name != "t3_control" or not isinstance(args, dict) or args.get("action") not in ADMITTING:
        return None
    environ = os.environ if environ is None else environ
    home = home or _home()
    if not home:
        return None
    data = _result(result)
    if data is None or (data.get("ok") is not True and data.get("outcome_unknown") is not True):
        return None
    job_id = environ.get(ENV_JOB) or _session_job(home, session_id, parent)
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        return None
    ok = data.get("ok") is True
    thread_id = data.get("thread_id") or (args.get("thread_id") if args["action"] == "send" else None)
    if thread_id is not None and (not isinstance(thread_id, str) or not thread_id or len(thread_id) > 600):
        return None
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    run = state.get("run") if isinstance(state.get("run"), dict) else {}
    count = run.get("count")
    record = {"job_id": job_id, "session_id": str(session_id or ""), "action": args["action"], "thread_id": thread_id,
              "confirmed": ok, "delivery": data.get("delivery") if args["action"] == "send" else "started",
              "outcome": state.get("outcome") if ok else None,
              "run_count": count if type(count) is int and ok else None,
              "active_id": run.get("active_id") if ok else None, "latest_id": run.get("latest_id") if ok else None,
              "at": clock()}
    record["key"] = _digest(f"{args['action']}:{args.get('request_id')}:{thread_id or args.get('cwd') or ''}")
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


def _ident(value):
    return value if isinstance(value, str) and value else None


def link_from(record):
    """The link an observation opens. A start's thread is new, so its run is the first one."""
    via = record.get("action")
    if not record.get("thread_id"):
        return Link(key=record["key"], thread_id="", via=via, confirmed=False, status=DROPPED,
                    admitted=float(record.get("at") or 0), error=LOST_START)
    confirmed = record.get("confirmed") is True
    queued = via == "send" and record.get("delivery") == "queued"
    count = record.get("run_count") if type(record.get("run_count")) is int else None
    active, latest = _ident(record.get("active_id")), _ident(record.get("latest_id"))
    live = record.get("outcome") in LIVE
    if via == "start":
        base, run_id, prior = 0, active, None
    elif queued:
        base, run_id, prior = count, None, active
    elif active is not None:  # a send to an idle thread: the active run is the one it started
        base, run_id, prior = None, active, None
    else:  # started but not yet visible: the run to tell it from is the thread's latest one
        base, run_id, prior = count, None, latest
    started = confirmed and run_id is not None or (confirmed and live and not queued and via == "start")
    unknown = confirmed and via == "send" and count is None and active is None and latest is None
    return Link(key=record["key"], thread_id=record["thread_id"], via=via, confirmed=confirmed, base_runs=base,
                queued=queued, started=started or unknown, status="running" if started and live else "queued",
                admitted=float(record.get("at") or 0), run_id=run_id, prior_run_id=prior)


def merge(link, record):
    """A repeated observation of the same request: only a confirmation changes the link."""
    if link.confirmed or record.get("confirmed") is not True:
        return link
    fresh = link_from(record)
    return replace(fresh, status=link.status if link.started else fresh.status, continues=link.continues)


def _final_text(messages):
    """The run's own reply: the thread's newest message when it is the assistant's. An older assistant message may
    belong to an earlier run, so a newest message from anyone else means no reply of this run is shown."""
    newest = next((m for m in reversed(messages or ()) if isinstance(m, dict)), None)
    if newest is not None and newest.get("role") == "assistant" and (newest.get("text") or "").strip():
        return newest["text"].strip()
    return ""


LATER_RUN = ("A later run of the same T3 thread started before Talk saw this run end, and T3 shows only the thread's "
             "latest run, so how this run ended is not known. Check the T3 thread.")


def _unknown(link, why):
    return replace(link, status=UNKNOWN, result="", error=why)


def _settled(link, outcome, read):
    """This run's outcome, read while it is the thread's latest run."""
    if outcome == "idle":
        return _unknown(link, "T3 shows the thread idle without saying how its run ended. Check the T3 thread.")
    text = _final_text(read.get("messages"))[-RESULT_CHARS:]
    return replace(link, status=outcome, result=text if outcome == "completed" else "",
                   error=text[-300:] if outcome == "failed" else "")


def assess(link, read, now):
    """The link after one T3 read. Nothing it cannot read is ever concluded."""
    if link.done or not isinstance(read, dict):
        return link
    if read.get("ok") is not True:
        if read.get("error") == "not_found":
            if not link.confirmed:
                return replace(link, status=DROPPED, seen=now, error="T3 has no such thread; nothing was admitted.")
            return _unknown(link, "T3 no longer has this thread, so how its run ended is not known.")
        return link
    state = read.get("state") if isinstance(read.get("state"), dict) else {}
    outcome = state.get("outcome")
    run = state.get("run") if isinstance(state.get("run"), dict) else {}
    count = run.get("count") if type(run.get("count")) is int else None
    active, latest = _ident(run.get("active_id")), _ident(run.get("latest_id"))
    live = outcome in LIVE
    run_id = link.run_id
    beyond = count is not None and link.base_runs is not None and count > link.base_runs + 1
    if run_id is None:
        candidate = active or latest
        # Only the one run after the admission point can be this link's; past it, which one was is not shown.
        if candidate is not None and candidate != link.prior_run_id and not beyond and (
                link.via == "start" or link.prior_run_id or link.base_runs is None or count is None
                or count > link.base_runs):
            run_id = candidate
    counted = count is not None and link.base_runs is not None and count > link.base_runs
    if not link.confirmed:
        # A start's thread id is derived from its request, so the thread exists only if T3 committed the launch.
        if link.via == "start" or run_id is not None or counted:
            link = replace(link, confirmed=True, seen=now)
        else:
            return replace(link, status=DROPPED, seen=now,
                           error="T3 never showed a run for the possibly sent message; it was not resent.")
    if run_id is not None:
        link = replace(link, run_id=run_id, started=True)
        if active == run_id:
            return replace(link, status="blocked" if outcome == "blocked" else "running")
        if latest not in (None, run_id) or beyond:
            return _unknown(link, LATER_RUN)
        return _settled(link, outcome, read) if outcome in SETTLED else replace(link, status="running")
    if beyond:  # several runs since the admission and none of them identified as this one
        return _unknown(replace(link, started=True, confirmed=True), LATER_RUN)
    # T3 shows no run ids: fall back to the run count and the thread's outcome.
    started = link.started or counted or (live and (not link.queued or link.base_runs is None))
    link = replace(link, started=started)
    if not started:
        return replace(link, status="queued")
    if outcome == "blocked":
        return replace(link, status="blocked")
    if live:
        return replace(link, status="running")
    return _settled(link, outcome, read) if outcome in SETTLED else link


def interruptible(link, read):
    """Whether T3's active run is this link's own run, so an interrupt stops nothing else."""
    run = ((read or {}).get("state") or {}).get("run") or {}
    return bool(link.run_id) and run.get("active_id") == link.run_id


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
