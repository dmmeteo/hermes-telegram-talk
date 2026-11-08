"""Background Hermes tasks started from a voice call: a typed job record and its state machine.

A job is keyed by its voice scope, the provider's delegation item id and the call generation that
received it. It runs as one native Hermes turn and is stored in the profile's own state.db meta table
through SessionDB.get_meta/set_meta, so it survives a Talk restart. The turn is a `hermes chat` process
(agent.py), or on cores that serve it an admission of the profile's canonical gateway (gateway_rpc.py),
which a restarted Talk adopts from its durable receipt.

States: accepted -> running -> succeeded | failed | cancelled | interrupted. An accepted job has not
started a process and may still be started after a restart; a running job whose process is gone is
interrupted and never rerun, because its side effects are unknown. Receipt is separate from state:
"delivered" means the result was handed to the voice model (a delegation append acknowledged by the
client, or call instructions the assistant then spoke in). It never means the result was heard.
The chat report of a finished job is a third, separate state (receipts.py); report_to holds the
destination bound when the job was accepted.
"""
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, replace

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

    @property
    def terminal(self):
        return self.state in TERMINAL

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
                "report": report.public() if report is not None else None}

    def spoken_state(self):
        return SPOKEN_STATE.get(self.state, "is " + self.state)

    def spoken(self):
        """One bounded sentence block describing the outcome, for the voice model."""
        head = f"Hermes task {self.number} ({self.request[:80]}) {self.spoken_state()}."
        if self.state == SUCCEEDED and self.result:
            return f"{head} Result: {self.result}"
        if self.state == FAILED and self.error:
            return f"{head} Reason: {self.error}"
        return head


def new_job(scope, conversation_id, voice_session_id, generation, delegation_id, number, request, now,
            report_to=None):
    return Job(id="jb_" + secrets.token_urlsafe(18), scope=scope, conversation_id=conversation_id,
               voice_session_id=voice_session_id, generation=generation, delegation_id=delegation_id,
               number=number, request=request, created=now, report_to=report_to)


def dump(job):
    return json.dumps(asdict(job), sort_keys=True)


def load(raw):
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    data["notes"] = tuple(data.get("notes") or ())
    try:
        return Job(**data)
    except TypeError:
        return None


def job_key(job_id):
    return JOB_PREFIX + job_id


def delegation_key(scope, conversation_id, generation, delegation_id):
    return f"hermes_talk:delegation:{scope}:{conversation_id}:{generation}:{delegation_id}"


class JobBoard:
    """Server-owned jobs for one profile: dedupe, bounded scheduling, delivery receipts and restart reconciliation.

    prompt_for(job) returns (prompt, resume session id); bind(job, native_session_id) records the native
    session a conversation's tasks continue in. desk (receipts.ReportDesk or None) gets every job's
    first terminal transition, with the full redacted result.
    """

    def __init__(self, history, agent, tasks, clock, prompt_for, bind, redact, desk=None):
        self.history, self.agent, self.tasks, self.clock = history, agent, tasks, clock
        self.prompt_for, self.bind, self.redact, self.desk = prompt_for, bind, redact, desk
        self.lock = threading.RLock()
        self.handles = {}
        self.revision = 0
        self.jobs = {}
        for _, raw in history.list_meta_prefix(JOB_PREFIX):
            job = load(raw)
            if job is not None:
                self.jobs[job.id] = job

    @property
    def gateway(self):
        return getattr(self.agent, "transport", "cli") == "gateway"

    def _save(self, job):
        self.jobs[job.id] = job
        self.history.set_meta(job_key(job.id), dump(job))
        self.revision += 1
        return job

    def _finish(self, job, full=None):
        """Persist a terminal job, then its report (before any send), then hand the report to the sender."""
        self.handles.pop(job.id, None)
        job = self._save(job)
        if self.desk is not None:
            try:
                self.desk.prepare(job, job.error if full is None else full)
                self.desk.dispatch(job.id)
            except Exception as exc:  # the job's own outcome is stored; a report failure must not strand the queue
                logger.warning("report for a finished task could not be prepared: %s", type(exc).__name__)
        return job

    def reconcile(self):
        """After a restart: adopt live runs, settle finished ones from their output, never rerun the rest."""
        if self.desk is not None:
            with self.lock:
                for job in self.jobs.values():
                    if job.terminal and self.desk.get(job.id) is None and (job.finished or 0) >= self.desk.since:
                        try:
                            self.desk.prepare(job, job.result if job.state == SUCCEEDED else job.error)
                        except Exception as exc:
                            logger.warning("report for a finished task could not be prepared: %s", type(exc).__name__)
            self.desk.reconcile()
        with self.lock:
            for job in list(self.jobs.values()):
                if job.state != RUNNING:
                    continue
                if self.gateway:
                    self._adopt_gateway(job)
                elif alive(job.pid, job.pid_start):
                    self._save(replace(job, notes=job.notes + ("adopted after restart",)))
                    threading.Thread(target=self._watch, args=(job.id, None), daemon=True).start()
                else:
                    self._settle(job.id, restarted=True)
        self.pump()

    def existing(self, scope, conversation_id, generation, delegation_id):
        """The job a delegation of this call already created, if any."""
        with self.lock:
            job = self.jobs.get(self.history.get_meta(delegation_key(scope, conversation_id, generation, delegation_id)) or "")
        return job if job is not None and job.scope == scope and job.conversation_id == conversation_id else None

    def submit(self, scope, conversation_id, voice_session_id, generation, delegation_id, request, report_to=None):
        """(job, created). A repeated delegation of the same call returns its existing job and starts nothing."""
        with self.lock:
            key = delegation_key(scope, conversation_id, generation, delegation_id)
            existing = self.jobs.get(self.history.get_meta(key) or "")
            if existing is not None and existing.scope == scope and existing.conversation_id == conversation_id:
                return existing, False
            if sum(1 for j in self.jobs.values() if j.scope == scope and j.state in OPEN) >= self.tasks.max_open_per_user:
                raise TalkError(429, "Too many Hermes tasks are already open. Wait for one to finish or cancel one.")
            number = 1 + max((j.number for j in self.jobs.values() if j.conversation_id == conversation_id), default=0)
            job = self._save(new_job(scope, conversation_id, voice_session_id, generation, delegation_id, number,
                                     request, self.clock(), report_to))
            self.history.set_meta(key, job.id)
        self.pump()
        return self.jobs[job.id], True

    def pump(self):
        """Start accepted jobs, oldest first, while slots are free; a job that failed to start takes no slot."""
        with self.lock:
            running = [j for j in self.jobs.values() if j.state == RUNNING]
            busy = {j.conversation_id for j in running}
            slots = self.tasks.max_concurrent - len(running)
            for job in sorted((j for j in self.jobs.values() if j.state == ACCEPTED), key=lambda j: (j.created, j.id)):
                if slots <= 0:
                    break
                if job.conversation_id in busy:
                    continue
                if self._launch(job):
                    slots -= 1
                    busy.add(job.conversation_id)

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

    def _launch(self, job):
        """True when a native process now runs the job; otherwise the job has failed and nothing ran."""
        if self.gateway:
            return self._launch_gateway(job)
        try:
            prompt, resume = self.prompt_for(job)
        except Exception as exc:
            self._finish(job.to(FAILED, finished=self.clock(),
                                error=f"The task could not be prepared ({type(exc).__name__}). Nothing ran."))
            return False
        # Persisted before the process exists: a crash from here on leaves a running job without a live
        # process, which reconciles to interrupted instead of a second run.
        job = self._save(job.to(RUNNING, started=self.clock()))
        try:
            process, output = self.agent.start(job.id, prompt, resume)
        except Exception as exc:
            self._finish(job.to(FAILED, finished=self.clock(),
                                error=f"The Hermes agent could not start ({type(exc).__name__}). Nothing ran."))
            return False
        self.handles[job.id] = process
        self._save(replace(job, pid=process.pid, pid_start=process_start(process.pid), output=output))
        threading.Thread(target=self._watch, args=(job.id, process), daemon=True).start()
        return True

    def _launch_gateway(self, job):
        """True when the gateway admitted the job; otherwise it has failed and nothing was admitted."""
        try:
            prompt, resume = self.prompt_for(job)
        except Exception as exc:
            self._finish(job.to(FAILED, finished=self.clock(),
                                error=f"The task could not be prepared ({type(exc).__name__}). Nothing ran."))
            return False
        job = self._save(job.to(RUNNING, started=self.clock()))
        try:
            self.agent.start(job.id, prompt, resume, lambda **f: self._persist(job.id, **f))
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
        threading.Thread(target=self._watch_gateway, args=(job.id,), daemon=True).start()
        return True

    def _watch_gateway(self, job_id):
        """Follow the admission until the gateway reports it settled; a lost connection is not an outcome.

        This one thread per job also drives its stop: a persisted cancel or a passed deadline is requested
        here, retried with a bounded backoff while the gateway is unreachable, and re-requested while the run
        has not settled. The job counts as stopped only once a terminal receipt and an idle session say so."""
        timed_out = False
        retry_at, backoff = 0.0, STOP_RETRY_MIN_S
        while True:
            job = self.jobs[job_id]
            if job.state != RUNNING:
                return
            if not job.admission_id and job.submission_text and job.native_session_id:
                try:  # unknown admission (lost answer or restart): the same id and text stay idempotent
                    self.agent.submit(job.id, job.native_session_id, job.submission_text,
                                      lambda **f: self._persist(job_id, **f))
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
            if (timed_out or job.cancel_requested) and job.admission_id and now >= retry_at:
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
        while True:
            job = self.jobs[job_id]
            if process is not None:
                if process.poll() is not None:
                    break
            elif not alive(job.pid, job.pid_start):
                break
            if not timed_out and self.clock() > (job.started or 0) + self.tasks.run_budget_s + 60:
                timed_out = True
                threading.Thread(target=self.agent.stop, args=(job.pid, job.pid_start), daemon=True).start()
            time.sleep(0.25)
        try:
            with self.lock:
                self._settle(job_id, timed_out=timed_out)
        finally:
            self.pump()

    def _settle(self, job_id, timed_out=False, restarted=False):
        job = self.jobs[job_id]
        if job.state != RUNNING:
            return
        if self.gateway:
            outcome = self.agent.outcome(job) if job.admission_id else None
        else:
            outcome = read_outcome(job.output) if job.output else None
        now = self.clock()
        if outcome is not None and outcome.session_id:
            job = replace(job, native_session_id=outcome.session_id)
            self.bind(job, outcome.session_id)
        limit = self.tasks.result_chars
        full = None
        if self.gateway and job.admission_id and outcome is None:
            # The gateway no longer knows the admission: neither a stop nor a result is proven.
            job = job.to(INTERRUPTED, finished=now, error="The Hermes gateway lost this task's run; its outcome is unknown.")
        elif timed_out:
            job = job.to(FAILED, finished=now, error="The task ran past its time budget and was stopped.")
        elif job.cancel_requested:
            job = job.to(CANCELLED, finished=now)
        elif outcome is None:
            job = job.to(INTERRUPTED, finished=now, error=("Talk restarted while the task ran; it was not rerun."
                                                           if restarted else "The run stopped without a result."))
        elif outcome.exit_code == 0 and outcome.text.strip():
            full = self.redact(outcome.text.strip())
            if outcome.text_cut:
                full += f"\n[{outcome.text_cut} more characters of this result were not kept]"
            job = job.to(SUCCEEDED, finished=now, result=clip(full, limit))
        elif outcome.exit_code == 130:
            job = job.to(INTERRUPTED, finished=now, error="The run was interrupted before it finished.")
        else:
            job = job.to(FAILED, finished=now, error=clip(self.redact(outcome.error or "Hermes reported a failure."), 300))
        self._finish(job, full)
        if not self.gateway:
            self.agent.discard_output(job.output)

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
        if not self.gateway:  # a gateway job's watcher drives the persisted stop intent, with retries
            threading.Thread(target=self.agent.stop, args=(job.pid, job.pid_start), daemon=True).start()
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
