"""Background Hermes tasks started from a voice call: a typed job record and its state machine.

A job is keyed by its voice scope, the provider's delegation item id and the call generation that
received it. It runs as one native Hermes turn (see agent.py) and is stored in the profile's own
state.db meta table through SessionDB.get_meta/set_meta, so it survives a Talk restart.

States: accepted -> running -> succeeded | failed | cancelled | interrupted. An accepted job has not
started a process and may still be started after a restart; a running job whose process is gone is
interrupted and never rerun, because its side effects are unknown. Receipt is separate from state:
"delivered" means the result was handed to the voice model (a delegation append acknowledged by the
client, or call instructions the assistant then spoke in). It never means the result was heard.
"""
import json
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, replace

from .agent import alive, process_start, read_outcome
from .store import TalkError, clip

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
    result: str = ""
    error: str = ""
    cancel_requested: bool = False
    acks: int = 0
    notes: tuple = field(default_factory=tuple)

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

    def public(self):
        """The browser's view: no prompt, process, native session or scope details."""
        return {"id": self.id, "number": self.number, "delegation_item_id": self.delegation_id,
                "voice_session_id": self.voice_session_id, "state": self.state, "receipt": self.receipt,
                "result": self.result if self.terminal else "", "error": self.error,
                "spoken": self.spoken() if self.terminal else ""}

    def spoken(self):
        """One bounded sentence block describing the outcome, for the voice model."""
        head = f"Hermes task {self.number} ({self.request[:80]}) {SPOKEN_STATE.get(self.state, 'is ' + self.state)}."
        if self.state == SUCCEEDED and self.result:
            return f"{head} Result: {self.result}"
        if self.state == FAILED and self.error:
            return f"{head} Reason: {self.error}"
        return head


def new_job(scope, conversation_id, voice_session_id, generation, delegation_id, number, request, now):
    return Job(id="jb_" + secrets.token_urlsafe(18), scope=scope, conversation_id=conversation_id,
               voice_session_id=voice_session_id, generation=generation, delegation_id=delegation_id,
               number=number, request=request, created=now)


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
    session a conversation's tasks continue in.
    """

    def __init__(self, history, agent, tasks, clock, prompt_for, bind, redact):
        self.history, self.agent, self.tasks, self.clock = history, agent, tasks, clock
        self.prompt_for, self.bind, self.redact = prompt_for, bind, redact
        self.lock = threading.RLock()
        self.handles = {}
        self.revision = 0
        self.jobs = {}
        for _, raw in history.list_meta_prefix(JOB_PREFIX):
            job = load(raw)
            if job is not None:
                self.jobs[job.id] = job

    def _save(self, job):
        self.jobs[job.id] = job
        self.history.set_meta(job_key(job.id), dump(job))
        self.revision += 1
        return job

    def reconcile(self):
        """After a restart: adopt live runs, settle finished ones from their output, never rerun the rest."""
        with self.lock:
            for job in list(self.jobs.values()):
                if job.state != RUNNING:
                    continue
                if alive(job.pid, job.pid_start):
                    self._save(replace(job, notes=job.notes + ("adopted after restart",)))
                    threading.Thread(target=self._watch, args=(job.id, None), daemon=True).start()
                else:
                    self._settle(job.id, restarted=True)
        self.pump()

    def submit(self, scope, conversation_id, voice_session_id, generation, delegation_id, request):
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
                                     request, self.clock()))
            self.history.set_meta(key, job.id)
        self.pump()
        return self.jobs[job.id], True

    def pump(self):
        with self.lock:
            running = [j for j in self.jobs.values() if j.state == RUNNING]
            busy = {j.conversation_id for j in running}
            for job in sorted((j for j in self.jobs.values() if j.state == ACCEPTED), key=lambda j: (j.created, j.id)):
                if len(running) >= self.tasks.max_concurrent:
                    break
                if job.conversation_id in busy:
                    continue
                self._launch(job)
                running.append(job)
                busy.add(job.conversation_id)

    def _launch(self, job):
        prompt, resume = self.prompt_for(job)
        # Persisted before the process exists: a crash from here on leaves a running job without a live
        # process, which reconciles to interrupted instead of a second run.
        job = self._save(job.to(RUNNING, started=self.clock()))
        try:
            process, output = self.agent.start(job.id, prompt, resume)
        except OSError as exc:
            self._save(job.to(FAILED, finished=self.clock(), error=f"The Hermes agent could not start ({type(exc).__name__})."))
            return
        self.handles[job.id] = process
        self._save(replace(job, pid=process.pid, pid_start=process_start(process.pid), output=output))
        threading.Thread(target=self._watch, args=(job.id, process), daemon=True).start()

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
        with self.lock:
            self._settle(job_id, timed_out=timed_out)
        self.pump()

    def _settle(self, job_id, timed_out=False, restarted=False):
        job = self.jobs[job_id]
        if job.state != RUNNING:
            return
        outcome = read_outcome(job.output) if job.output else None
        now = self.clock()
        if outcome is not None and outcome.session_id:
            job = replace(job, native_session_id=outcome.session_id)
            self.bind(job, outcome.session_id)
        limit = self.tasks.result_chars
        if timed_out:
            job = job.to(FAILED, finished=now, error="The task ran past its time budget and was stopped.")
        elif job.cancel_requested:
            job = job.to(CANCELLED, finished=now)
        elif outcome is None:
            job = job.to(INTERRUPTED, finished=now, error=("Talk restarted while the task ran; it was not rerun."
                                                           if restarted else "The run stopped without a result."))
        elif outcome.exit_code == 0 and outcome.text.strip():
            job = job.to(SUCCEEDED, finished=now, result=clip(self.redact(outcome.text), limit))
        elif outcome.exit_code == 130:
            job = job.to(INTERRUPTED, finished=now, error="The run was interrupted before it finished.")
        else:
            job = job.to(FAILED, finished=now, error=clip(self.redact(outcome.error or "Hermes reported a failure."), 300))
        self.handles.pop(job_id, None)
        self._save(job)
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
                return self._save(job.to(CANCELLED, finished=self.clock()))
            if job.state != RUNNING or job.cancel_requested:
                return job
            job = self._save(replace(job, cancel_requested=True))
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
        with self.lock:
            jobs = [j for j in self.jobs.values() if j.scope == scope and j.conversation_id == conversation_id]
        return sorted(jobs, key=lambda j: j.number)[-limit:]

    def wait_idle(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                jobs = list(self.jobs.values())
            if not any(j.state in OPEN for j in jobs):
                return True
            time.sleep(0.2)
        return False
