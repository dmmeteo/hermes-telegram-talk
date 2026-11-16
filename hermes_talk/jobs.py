"""Background Hermes tasks started from a voice call: a typed job record and its state machine.

A job is keyed by its voice scope, the provider's delegation item id and the call generation that
received it. It runs as one native Hermes turn and is stored in the profile's own state.db meta table
through SessionDB.get_meta/set_meta, so it survives a Talk restart. The turn is a `hermes chat` process
(agent.py), or on cores that serve it an admission of the profile's canonical gateway (gateway_rpc.py),
which a restarted Talk adopts from its durable receipt.

States: accepted -> running -> succeeded | failed | cancelled | interrupted. An accepted job has not
started a process and may still be started after a restart; a running job whose process is gone is
interrupted and never rerun, because its side effects are unknown. A conversation's tasks continue its one
native session, which takes one turn at a time; a job started while another task of its conversation holds
that session is `parallel` and runs in a fresh native session of its own. Receipt is separate from state:
"delivered" means the result was handed to the voice model (a delegation append acknowledged by the
client, or call instructions the assistant then spoke in). It never means the result was heard.
The job's Telegram status message is a third, separate state (receipts.py), updated from every stored
version of the job; report_to and title hold its destination and short name, bound when it was accepted.

A job is large once T3 admitted work from its own turn (work.py): its `links`. Its turn then holds no execution
slot, and when the turn ends the job stays running on its T3 work: `turn` keeps what the turn alone would have
settled to, and the job settles only from what T3 shows. Everything newer than the base record (`parallel`, `links`,
`turn`, `unknown`) is stored under its own key, so a Talk from before these fields still loads every job it wrote.
`unknown` marks a job whose T3 run ended without T3 showing how: its base state is interrupted (a state every reader
knows), it carries no result, and it is spoken and posted as unknown, never as completed. That includes a run Talk
failed to read from T3 for `unread_s`. A T3 call the core stopped waiting for leaves a note link: the job settles from
its turn and its reason says T3 may hold work Talk cannot see.
"""
import errno
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, replace

from . import work
from .agent import alive, process_start, read_outcome
from .store import TalkError, clip

logger = logging.getLogger("tg-voice")

ACCEPTED, RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED = (
    "accepted", "running", "succeeded", "failed", "cancelled", "interrupted")
TRANSITIONS = {
    ACCEPTED: {RUNNING, CANCELLED, FAILED},
    RUNNING: {SUCCEEDED, FAILED, CANCELLED, INTERRUPTED},
}
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED, INTERRUPTED})
OPEN = frozenset({ACCEPTED, RUNNING})
UNDELIVERED, DELIVERED = "undelivered", "delivered"
DELIVERY_CHANNELS = ("delegation", "context", "instructions")
JOB_ID = re.compile(r"jb_[A-Za-z0-9_-]{16,64}")
DELEGATION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
JOB_PREFIX = "hermes_talk:job:"
WORK_PREFIX = "hermes_talk:jobwork:"
EXTRA = ("parallel", "links", "turn", "unknown")
FOLLOW_S = 10.0  # how often open T3 work is read
INTAKE_S = 1.0  # how often a running turn's admissions are picked up
STOP_RETRY_MIN_S, STOP_RETRY_MAX_S, STOP_RECHECK_S = 1.0, 8.0, 10.0  # gateway stop: retry backoff, re-request
SPOKEN_STATE = {SUCCEEDED: "finished", FAILED: "failed", CANCELLED: "was cancelled",
                INTERRUPTED: "was interrupted when Talk restarted; it was not rerun"}


class TransitionError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    id: str
    scope: str
    conversation_id: str
    voice_session_id: str
    generation: int
    delegation_id: str
    number: int
    request: str
    state: str = ACCEPTED
    receipt: str = UNDELIVERED
    delivered_via: str = None
    created: float = 0.0
    started: float = None
    finished: float = None
    pid: int = None
    pid_start: str = None
    output: str = None
    native_session_id: str = None
    admission_id: str = None
    submission_text: str = None
    result: str = ""
    error: str = ""
    cancel_requested: bool = False
    acks: int = 0
    notes: tuple = field(default_factory=tuple)
    report_to: dict = None
    title: str = ""
    parallel: bool = False
    links: tuple = ()
    turn: dict = None
    unknown: bool = False

    @property
    def terminal(self):
        return self.state in TERMINAL

    @property
    def large(self):
        return any(link.counts for link in self.links)

    @property
    def unseen(self):
        """It ended on its turn alone while T3 may hold work of it that Talk cannot see."""
        return self.terminal and not self.large and any(work.unseen(link) for link in self.links)

    @property
    def kind(self):
        return "large" if self.large else "micro"

    @property
    def turn_open(self):
        """The job's own Hermes turn runs (it holds a process or a gateway admission)."""
        return self.state == RUNNING and self.turn is None

    @property
    def work_status(self):
        return work.work_status(self.links) if self.state == RUNNING else None

    def to(self, state, **fields):
        if state not in TRANSITIONS.get(self.state, ()):
            raise TransitionError(f"{self.state} -> {state}")
        return replace(self, state=state, **fields)

    def delivered(self, via, now):
        if not self.terminal:
            raise TransitionError("only a finished job has a result to deliver")
        if via not in DELIVERY_CHANNELS:
            raise ValueError(via)
        if self.receipt == DELIVERED:
            return replace(self, acks=self.acks + 1)
        return replace(self, receipt=DELIVERED, delivered_via=via, acks=self.acks + 1,
                       notes=self.notes + (f"delivered:{via}:{int(now)}",))

    def public(self, report=None):
        """The browser's view: no prompt, process, native session, scope or destination details."""
        return {"id": self.id, "number": self.number, "delegation_item_id": self.delegation_id,
                "voice_session_id": self.voice_session_id, "state": self.state, "receipt": self.receipt,
                "result": self.result if self.terminal else "", "error": self.error,
                "spoken": self.spoken() if self.terminal else "",
                "report": report.public() if report is not None else None,
                "kind": self.kind, "work": self.work_status, "outcome_unknown": self.unknown}

    def spoken_state(self):
        if self.unknown:
            return "ended in T3, but how it ended is not known"
        if self.state == RUNNING and self.large:
            return {"blocked": "is waiting for an answer or approval in T3",
                    "queued": "is queued in T3"}.get(self.work_status, "is running as T3 code work")
        return SPOKEN_STATE.get(self.state, "is " + self.state)

    def spoken(self):
        """One bounded sentence block describing the outcome, for the voice model."""
        head = f"Hermes task {self.number} ({self.request[:80]}) {self.spoken_state()}."
        if self.state == SUCCEEDED and self.result:
            return f"{head} Result: {self.result}"
        if (self.state == FAILED or self.unknown or self.unseen) and self.error:
            return f"{head} Reason: {self.error}"
        return head


def new_job(scope, conversation_id, voice_session_id, generation, delegation_id, number, request, now,
            report_to=None, title=""):
    return Job(id="jb_" + secrets.token_urlsafe(18), scope=scope, conversation_id=conversation_id,
               voice_session_id=voice_session_id, generation=generation, delegation_id=delegation_id,
               number=number, request=request, created=now, report_to=report_to, title=title)


def dump(job):
    """The base record: exactly the fields every earlier Talk reads, so a rollback still loads the job."""
    data = asdict(job)
    for key in EXTRA:
        del data[key]
    return json.dumps(data, sort_keys=True)


def dump_extra(job):
    """The newer fields, under their own key; None while all of them are unset."""
    if not (job.parallel or job.links or job.turn or job.unknown):
        return None
    return json.dumps({"parallel": job.parallel, "links": [work.link_dump(l) for l in job.links], "turn": job.turn,
                       "unknown": job.unknown}, sort_keys=True)


def _loads(raw):
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def load(raw, extra=None):
    data = _loads(raw)
    if data is None:
        return None
    data["notes"] = tuple(data.get("notes") or ())
    more = _loads(extra) or {}
    data["parallel"] = bool(more.get("parallel", data.get("parallel", False)))
    links = [work.link_load(item) for item in more.get("links") or () if isinstance(item, dict)]
    data["links"] = tuple(link for link in links if link is not None)
    data["turn"] = more.get("turn") if isinstance(more.get("turn"), dict) else None
    data["unknown"] = more.get("unknown") is True
    try:
        return Job(**data)
    except TypeError:
        return None


def job_key(job_id):
    return JOB_PREFIX + job_id


def work_key(job_id):
    return WORK_PREFIX + job_id


def delegation_key(scope, conversation_id, generation, delegation_id):
    return f"hermes_talk:delegation:{scope}:{conversation_id}:{generation}:{delegation_id}"


class JobBoard:
    """Server-owned jobs for one profile: dedupe, bounded scheduling, delivery receipts and restart reconciliation.

    prompt_for(job) returns (prompt, resume session id); bind(job, native_session_id) records the native
    session a conversation's tasks continue in. desk (receipts.ReportDesk or None) follows every stored
    version of every job. home is the profile home whose work folder the post_tool_call hook writes; t3
    (work.T3) reads and interrupts the T3 threads large jobs follow.

    Execution: at most tasks.max_concurrent turns of jobs that are not large run at once, oldest first. Waiting
    jobs are not capped; a large job's turn and its T3 work hold no slot. At most tasks.max_concurrent turns of large
    jobs stay live beside them: past that, the oldest such turn is stopped (its T3 work goes on), so live turns stay
    bounded at twice the setting.
    """

    def __init__(self, history, agent, tasks, clock, prompt_for, bind, redact, desk=None, home=None, t3=None,
                 follow_s=FOLLOW_S, unread_s=work.UNREADABLE_S):
        self.history, self.agent, self.tasks, self.clock = history, agent, tasks, clock
        self.prompt_for, self.bind, self.redact, self.desk = prompt_for, bind, redact, desk
        self.home, self.t3, self.follow_s, self.unread_s = home, t3 or work.T3(), follow_s, unread_s
        self.lock = threading.RLock()
        self.handles = {}
        self.revision = 0
        self.jobs = {}
        self.following = False
        self.coordinating = False
        self.wake = threading.Event()
        self.retry_at = 0.0
        self.interrupts = {}  # link key -> monotonic time of the last T3 interrupt asked for a cancelled job
        self.reaped = set()  # large jobs whose still-running turn is being stopped to keep live turns bounded
        extras = dict(history.list_meta_prefix(WORK_PREFIX))
        for key, raw in history.list_meta_prefix(JOB_PREFIX):
            job = load(raw, extras.get(WORK_PREFIX + key[len(JOB_PREFIX):]))
            if job is not None:
                self.jobs[job.id] = job

    @property
    def gateway(self):
        return getattr(self.agent, "transport", "cli") == "gateway"

    def _save(self, job):
        with self.lock:
            self.jobs[job.id] = job
            self.history.set_meta(job_key(job.id), dump(job))
            extra = dump_extra(job)
            self.history.set_meta(work_key(job.id), extra or "{}")
            self.revision += 1
            self._report(job)
            return job

    def _report(self, job):
        """The job is stored first; its status message follows. A report failure must not strand the queue."""
        if self.desk is None:
            return
        try:
            self.desk.follow(job)
        except Exception as exc:
            logger.warning("a task's status message could not be updated: %s", type(exc).__name__)

    def _finish(self, job):
        self.handles.pop(job.id, None)
        return self._save(job)

    def reconcile(self):
        """After a restart: adopt live runs, settle finished ones from their output, never rerun the rest."""
        if self.desk is not None:
            with self.lock:
                for job in self.jobs.values():
                    self._report(job)
            self.desk.reconcile()
        with self.lock:
            for job in list(self.jobs.values()):
                if job.state != RUNNING:
                    continue
                if job.turn is not None:
                    continue  # its turn ended before the restart; only its T3 work is followed
                if self.gateway:
                    self._adopt_gateway(job)
                elif alive(job.pid, job.pid_start):
                    self._save(replace(job, notes=job.notes + ("adopted after restart",)))
                    threading.Thread(target=self._watch, args=(job.id, None), daemon=True).start()
                else:
                    self._settle(job.id, restarted=True)
        self._follow_soon()
        self.pump()

    def existing(self, scope, conversation_id, generation, delegation_id):
        """The job a delegation of this call already created, if any."""
        with self.lock:
            job = self.jobs.get(self.history.get_meta(delegation_key(scope, conversation_id, generation, delegation_id)) or "")
        return job if job is not None and job.scope == scope and job.conversation_id == conversation_id else None

    def submit(self, scope, conversation_id, voice_session_id, generation, delegation_id, request, report_to=None,
               title=""):
        """(job, created). A repeated delegation of the same call returns its existing job and starts nothing."""
        with self.lock:
            key = delegation_key(scope, conversation_id, generation, delegation_id)
            existing = self.jobs.get(self.history.get_meta(key) or "")
            if existing is not None and existing.scope == scope and existing.conversation_id == conversation_id:
                return existing, False
            number = 1 + max((j.number for j in self.jobs.values() if j.conversation_id == conversation_id), default=0)
            created = max(self.clock(), max((j.created for j in self.jobs.values()), default=0) + 0.000001)
            job = self._save(new_job(scope, conversation_id, voice_session_id, generation, delegation_id, number,
                                     request, created, report_to, title))
            self.history.set_meta(key, job.id)
        self.pump()
        return self.jobs[job.id], True

    def pump(self):
        """Wake independent dispatch; intake never prepares prompts or waits for a native launch."""
        with self.lock:
            self.wake.set()
            if self.coordinating:
                return
            self.coordinating = True
            try:
                threading.Thread(target=self._coordinate, name="talk-task-coordinator", daemon=True).start()
            except RuntimeError:
                self.coordinating = False
                logger.warning("task coordinator could not start; accepted requests remain queued")

    def _coordinate(self):
        while True:
            self.wake.wait(0.5)
            self.wake.clear()
            with self.lock:
                waiting = sorted((j for j in self.jobs.values() if j.state == ACCEPTED), key=lambda j: (j.created, j.id))
                turns = [j for j in self.jobs.values() if j.turn_open]
                promoted = sorted((j for j in turns if j.large),
                                  key=lambda j: min(link.admitted for link in j.links if link.counts))
                for job in promoted[:max(0, len(promoted) - self.tasks.max_concurrent)]:
                    self._reap(job)
                if not waiting:
                    self.coordinating = False
                    return
                if time.monotonic() < self.retry_at:
                    continue
                held = {j.conversation_id for j in turns if not j.parallel}
                slots = self.tasks.max_concurrent - sum(not j.large for j in turns)
                for job in waiting[:max(0, slots)]:
                    running = self._save(job.to(RUNNING, started=self.clock(), parallel=job.conversation_id in held))
                    try:
                        threading.Thread(target=self._run, args=(running.id,), name="talk-task", daemon=True).start()
                    except RuntimeError:
                        self._save(job)
                        self.retry_at = time.monotonic() + 1
                        break
                    held.add(job.conversation_id)

    def _run(self, job_id):
        try:
            job = self.jobs[job_id]
            if self._launch(job):
                if self.gateway:
                    self._watch_gateway(job_id)
                else:
                    self._watch(job_id, self.handles[job_id])
        finally:
            self.pump()

    def _defer(self, job):
        with self.lock:
            current = self.jobs[job.id]
            if current.cancel_requested:
                self._finish(current.to(CANCELLED, finished=self.clock()))
            else:
                self._save(replace(current, state=ACCEPTED, started=None, parallel=False))
                self.retry_at = time.monotonic() + 1

    def _reap(self, job):
        """Stop a large job's turn that is still live; its T3 work is followed as before (the lock is held)."""
        if job.id in self.reaped:
            return
        self.reaped.add(job.id)
        logger.info("stopping the turn of a task whose T3 work is followed, to keep live turns bounded")

    def _adopt_gateway(self, job):
        """A gateway job outlives Talk: follow its admission, or admit it once if Talk stopped before the receipt."""
        if not job.native_session_id:
            self._settle(job.id, restarted=True)  # the session is stored before submission, so nothing was admitted
            return
        job = self._save(replace(job, notes=job.notes + ("adopted after restart",)))
        if not job.admission_id and not job.submission_text:
            self._settle(job.id, restarted=True)
            return
        # Without an admission the watcher resubmits the same id and text: the gateway returns the earlier
        # admission if it took one, and only its own refusal fails the job.
        threading.Thread(target=self._watch_gateway, args=(job.id,), daemon=True).start()

    def _persist(self, job_id, **fields):
        with self.lock:
            return self._save(replace(self.jobs[job_id], **fields))

    def _persist_gateway(self, job_id, **fields):
        """A gateway turn runs in the gateway process: its session marker tells the hook there which job it is."""
        job = self._persist(job_id, **fields)
        if fields.get("native_session_id") and self.home is not None:
            work.mark_session(self.home, fields["native_session_id"], job_id)
        return job

    def _launch(self, job):
        """True when a native process now runs the job; otherwise the job has failed and nothing ran."""
        if self.gateway:
            return self._launch_gateway(job)
        try:
            prompt, resume = self._prompt(job)
        except Exception as exc:
            self._finish(job.to(FAILED, finished=self.clock(),
                                error=f"The task could not be prepared ({type(exc).__name__}). Nothing ran."))
            return False
        # Persisted before the process exists: a crash from here on leaves a running job without a live
        # process, which reconciles to interrupted instead of a second run.
        if self._cancel_before_launch(job.id):
            return False
        try:
            process, output = self.agent.start(job.id, prompt, resume)
        except Exception as exc:
            if isinstance(exc, OSError) and exc.errno in (errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE):
                self._defer(job)
                return False
            self._finish(job.to(FAILED, finished=self.clock(),
                                error=f"The Hermes agent could not start ({type(exc).__name__}). Nothing ran."))
            return False
        self.handles[job.id] = process
        self._persist(job.id, pid=process.pid, pid_start=process_start(process.pid), output=output)
        return True

    def _cancel_before_launch(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if not job.cancel_requested:
                return False
            self._finish(job.to(CANCELLED, finished=self.clock()))
            return True

    def _prompt(self, job):
        prompt, resume = self.prompt_for(job)
        return prompt, None if job.parallel else resume

    def _launch_gateway(self, job):
        """True when the gateway admitted the job; otherwise it has failed and nothing was admitted."""
        try:
            prompt, resume = self._prompt(job)
        except Exception as exc:
            self._finish(job.to(FAILED, finished=self.clock(),
                                error=f"The task could not be prepared ({type(exc).__name__}). Nothing ran."))
            return False
        if self._cancel_before_launch(job.id):
            return False
        try:
            self.agent.start(job.id, prompt, resume, lambda **f: self._persist_gateway(job.id, **f))
        except Exception as exc:
            job = self.jobs[job.id]
            submitted = job.native_session_id and job.submission_text
            if job.admission_id is None and (getattr(exc, "refused", False) or not submitted):
                # The gateway refused it, or nothing was submitted (no session stored): nothing ran.
                self._finish(job.to(FAILED, finished=self.clock(),
                                    error=f"The Hermes gateway did not take the task ({type(exc).__name__})."))
                return False
            if job.admission_id is None:
                # The answer was lost, not refused: the gateway may run it. The watcher resubmits the stored
                # session, text and submission id, which returns that admission instead of a second run.
                logger.warning("a task's admission is unknown after %s; reconciling", type(exc).__name__)
        return True

    def _watch_gateway(self, job_id):
        """Follow the admission until the gateway reports it settled; a lost connection is not an outcome.

        This one thread per job also drives its stop: a persisted cancel or a passed deadline is requested
        here, retried with a bounded backoff while the gateway is unreachable, and re-requested while the run
        has not settled. The job counts as stopped only once a terminal receipt and an idle session say so."""
        timed_out = False
        retry_at, backoff = 0.0, STOP_RETRY_MIN_S
        intake_at = 0.0
        while True:
            job = self.jobs[job_id]
            if not job.turn_open:
                return
            if time.monotonic() >= intake_at:
                intake_at = time.monotonic() + INTAKE_S
                self._take(job_id)
            if not job.admission_id and job.submission_text and job.native_session_id:
                try:  # unknown admission (lost answer or restart): the same id and text stay idempotent
                    self.agent.submit(job.id, job.native_session_id, job.submission_text,
                                      lambda **f: self._persist_gateway(job_id, **f))
                except Exception as exc:
                    if getattr(exc, "refused", False):
                        with self.lock:
                            job = self.jobs[job_id]
                            if job.state == RUNNING and not job.admission_id:
                                self._finish(job.to(FAILED, finished=self.clock(),
                                                    error="The Hermes gateway did not take the task."))
                        self.pump()
                        return
                job = self.jobs[job_id]
            if job.admission_id and self.agent.settled(job):
                break
            if not timed_out and self.clock() > (job.started or 0) + self.tasks.run_budget_s + 60:
                timed_out = True
            now = time.monotonic()
            if (timed_out or job.cancel_requested or job_id in self.reaped) and job.admission_id and now >= retry_at:
                # Single-flight (this thread only) and bounded: never more often than the backoff allows.
                delivered = self.agent.stop(job)
                backoff = STOP_RETRY_MIN_S if delivered else min(backoff * 2, STOP_RETRY_MAX_S)
                retry_at = time.monotonic() + (STOP_RECHECK_S if delivered else backoff)
            time.sleep(0.5)
        try:
            with self.lock:
                self._settle(job_id, timed_out=timed_out)
        finally:
            self.pump()

    def _watch(self, job_id, process):
        timed_out = False
        stopping = False
        intake_at = 0.0
        while True:
            job = self.jobs[job_id]
            if time.monotonic() >= intake_at:
                intake_at = time.monotonic() + INTAKE_S
                self._take(job_id)
            if process is not None:
                if process.poll() is not None:
                    break
            elif not alive(job.pid, job.pid_start):
                break
            if not timed_out and self.clock() > (job.started or 0) + self.tasks.run_budget_s + 60:
                timed_out = True
            if not stopping and (timed_out or job.cancel_requested or job_id in self.reaped):
                stopping = True
                self.agent.stop(job.pid, job.pid_start, wait=(lambda _: process.poll()) if process is not None else None)
            time.sleep(0.25)
        try:
            with self.lock:
                self._settle(job_id, timed_out=timed_out)
        finally:
            self.pump()

    def _settle(self, job_id, timed_out=False, restarted=False):
        """The turn ended: settle the job from it, or, when T3 took work from it, keep following that work."""
        job = self._intake(job_id)
        if not job.turn_open:
            return
        if self.gateway:
            outcome = self.agent.outcome(job) if job.admission_id else None
        else:
            outcome = read_outcome(job.output) if job.output else None
        now = self.clock()
        if outcome is not None and outcome.session_id:
            job = replace(job, native_session_id=outcome.session_id)
            if not job.parallel:
                self.bind(job, outcome.session_id)
        state, fields = self._turn_outcome(job, outcome, timed_out, restarted)
        self.reaped.discard(job.id)
        if job.links:
            # The admission moves into `turn`: a rollback to a Talk before T3 work then finds no admission to settle
            # this still-running job from, and marks it interrupted instead of completed with the turn's reply.
            job = self._save(replace(job, turn={"state": state, "at": now, "admission_id": job.admission_id, **fields},
                                     admission_id=None, submission_text=None))
            self.handles.pop(job.id, None)
            self._conclude(job.id)
            self._follow_soon()
        else:
            job = self._finish(job.to(state, finished=now, **fields))
        if self.gateway:
            if job.native_session_id and self.home is not None:
                work.unmark_session(self.home, job.native_session_id, job.id)
        else:
            self.agent.discard_output(job.output)

    def _turn_outcome(self, job, outcome, timed_out, restarted):
        """(state, fields) the turn alone settles the job to."""
        limit = self.tasks.result_chars
        if self.gateway and job.admission_id and outcome is None:
            # The gateway no longer knows the admission: neither a stop nor a result is proven.
            return INTERRUPTED, {"error": "The Hermes gateway lost this task's run; its outcome is unknown."}
        if timed_out:
            return FAILED, {"error": "The task ran past its time budget and was stopped."}
        if job.cancel_requested:
            return CANCELLED, {}
        if outcome is None:
            return INTERRUPTED, {"error": ("Talk restarted while the task ran; it was not rerun."
                                           if restarted else "The run stopped without a result.")}
        if outcome.exit_code == 0 and outcome.text.strip():
            return SUCCEEDED, {"result": clip(self.redact(outcome.text.strip()), limit)}
        if outcome.exit_code == 130:
            return INTERRUPTED, {"error": "The run was interrupted before it finished."}
        return FAILED, {"error": clip(self.redact(outcome.error or "Hermes reported a failure."), 300)}

    def _take(self, job_id):
        with self.lock:
            return self._intake(job_id)

    def _intake(self, job_id):
        """Links for the admissions the hook recorded for this job's turn (idempotent; the lock is held)."""
        job = self.jobs[job_id]
        if self.home is None or job.state != RUNNING:
            return job
        links = {link.key: link for link in job.links}
        changed = False
        for record in work.observations(self.home, job_id):
            if record.get("answered"):  # a cut call whose retry T3 answered: the admission's own link stands for it
                changed = links.pop(record["key"], None) is not None or changed
                continue
            known = links.get(record["key"])
            link = work.merge(known, record) if known is not None else work.link_from(record)
            if known is None:
                owner = self._owner(job, link.thread_id)
                link = replace(link, continues=owner.number if owner is not None else None)
            if link != known:
                links[record["key"]] = link
                changed = True
        if not changed:
            return job
        job = self._save(replace(job, links=tuple(sorted(links.values(), key=lambda l: (l.admitted, l.key)))))
        self._follow_soon()
        return job

    def _owner(self, job, thread_id):
        """The earlier task of the same account whose work is this T3 thread: a send to it continues that work."""
        owners = [j for j in self.jobs.values() if j.scope == job.scope and j.id != job.id and j.number
                  and any(link.thread_id == thread_id and link.counts for link in j.links)]
        return min(owners, key=lambda j: j.created) if owners else None

    def _conclude(self, job_id):
        """Settle a job whose turn ended once all its T3 work settled (the lock is held)."""
        job = self.jobs[job_id]
        if job.state != RUNNING or job.turn is None or not all(link.done for link in job.links):
            return
        now, turn = self.clock(), job.turn
        counted = [link for link in job.links if link.counts]
        statuses = {link.status for link in counted}
        if not counted:  # every possible admission proved empty: the turn's own outcome stands
            notes = dict.fromkeys(l.error for l in job.links if work.unseen(l) and l.error)
            lost = " " + " ".join(notes) if notes else ""
            result, error = turn.get("result", ""), turn.get("error", "")
            if lost and turn["state"] == SUCCEEDED:
                result = clip(result + lost, self.tasks.result_chars)
            elif lost:
                error = clip(error + lost, 300)
            job = job.to(turn["state"], finished=now, result=result, error=error)
        elif job.cancel_requested and "interrupted" in statuses:
            job = job.to(CANCELLED, finished=now)
        elif "failed" in statuses:
            reason = next((l.error for l in counted if l.status == "failed" and l.error), "")
            job = job.to(FAILED, finished=now, error=clip(self.redact(
                "The T3 work failed." + (f" {reason}" if reason else "")), 300))
        elif "interrupted" in statuses:
            job = job.to(INTERRUPTED, finished=now, error="The T3 run was interrupted before it finished.")
        elif work.UNKNOWN in statuses:  # no result is claimed, and no other run's text is borrowed
            reason = next(l.error for l in counted if l.status == work.UNKNOWN)
            job = job.to(INTERRUPTED, finished=now, unknown=True, error=clip(self.redact(reason), 300))
        else:
            text = next((l.result for l in reversed(counted) if l.result), "") or turn.get("result", "")
            job = job.to(SUCCEEDED, finished=now, result=clip(self.redact(
                text or "The T3 work finished without a final message."), self.tasks.result_chars))
        self._finish(job)

    def _follow_soon(self):
        with self.lock:
            if self.following or not any(j.state == RUNNING and j.links for j in self.jobs.values()):
                return
            self.following = True
        threading.Thread(target=self._follow, name="talk-t3-follow", daemon=True).start()

    def _follow(self):
        """One thread reads every open T3 link of every running job, until none is left."""
        try:
            while True:
                with self.lock:
                    ids = [j.id for j in self.jobs.values() if j.state == RUNNING and j.links]
                    if not ids:
                        self.following = False
                        return
                for job_id in ids:
                    try:
                        self._follow_job(job_id)
                    except Exception as exc:  # one job's failed read or store must not stop following the others
                        logger.warning("T3 work of a task could not be followed this round: %s", type(exc).__name__)
                try:
                    self.pump()
                except Exception as exc:
                    logger.warning("queued tasks could not be started this round: %s", type(exc).__name__)
                time.sleep(self.follow_s)
        except BaseException:
            with self.lock:
                self.following = False
            raise

    def _follow_job(self, job_id):
        job = self._take(job_id)
        if job.state != RUNNING:
            return
        now = self.clock()
        assessed = {}
        for link in job.links:
            if link.done:
                continue
            raw = self.t3.read(link.thread_id)
            fresh = work.assess(link, raw, now, self.unread_s)
            assessed[link.key] = (link, fresh)
            # Only this link's own run is interrupted, never one another task queued on the same thread.
            if job.cancel_requested and fresh.counts and fresh.status in ("running", "blocked") \
                    and work.interruptible(fresh, raw):
                last = self.interrupts.get(link.key)
                if last is None or time.monotonic() - last >= STOP_RECHECK_S:
                    self.interrupts[link.key] = time.monotonic()
                    self.t3.interrupt(link.thread_id)  # confirmed only by a later read showing the run settled
        with self.lock:
            job = self.jobs[job_id]
            if job.state != RUNNING:
                return
            # A link that changed meanwhile (a retried call confirmed it) keeps its new value until the next round.
            links = tuple(assessed[l.key][1] if l.key in assessed and assessed[l.key][0] == l else l for l in job.links)
            if links != job.links:
                self._save(replace(job, links=links))
            self._conclude(job_id)

    def owned(self, scope, conversation_id, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise TalkError(400, "Malformed task id.")
        job = self.jobs.get(job_id)
        if job is None or job.scope != scope or job.conversation_id != conversation_id:
            raise TalkError(404, "That task is not available.")
        return job

    def by_number(self, scope, conversation_id, number):
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            if job.scope == scope and job.conversation_id == conversation_id and job.number == number:
                return job
        raise TalkError(404, "That task is not available.")

    def cancel(self, scope, conversation_id, job_id):
        """Cancel is idempotent; a finished task keeps its outcome."""
        with self.lock:
            job = self.owned(scope, conversation_id, job_id)
            if job.state == ACCEPTED:
                return self._finish(job.to(CANCELLED, finished=self.clock()))
            if job.state != RUNNING or job.cancel_requested:
                return job
            job = self._save(replace(job, cancel_requested=True))
        # A gateway job's watcher drives the persisted stop intent, with retries; T3 work is interrupted by the
        # follower, and the job is cancelled only once T3 shows its run stopped.
        self._follow_soon()
        return job

    def ack(self, scope, conversation_id, voice_session_id, job_id):
        with self.lock:
            job = self.owned(scope, conversation_id, job_id)
            if job.voice_session_id != voice_session_id:
                raise TalkError(409, "That task belongs to another call.")
            if not job.terminal:
                raise TalkError(409, "That task has no result yet.")
            return self._save(job.delivered("delegation", self.clock()))

    def mark_delivered(self, job_ids, via):
        with self.lock:
            for job_id in job_ids:
                job = self.jobs.get(job_id)
                if job is not None and job.terminal and job.receipt == UNDELIVERED:
                    self._save(job.delivered(via, self.clock()))

    def of_conversation(self, scope, conversation_id, limit=10):
        """The conversation's open and not yet delivered jobs, whatever their age, plus its latest `limit`."""
        with self.lock:
            jobs = sorted((j for j in self.jobs.values() if j.scope == scope and j.conversation_id == conversation_id),
                          key=lambda j: j.number)
        recent = {j.id for j in jobs[-limit:]}
        return [j for j in jobs if j.id in recent or j.state in OPEN or (j.terminal and j.receipt == UNDELIVERED)]

    def wait_idle(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                jobs = list(self.jobs.values())
            if not any(j.state in OPEN for j in jobs):
                return True
            time.sleep(0.2)
        return False
