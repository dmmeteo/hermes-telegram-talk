"""Runs voice tasks as native turns of the profile's canonical Hermes gateway, through the core's public client.

Cores whose CLI rejects `--run-budget` serve the canonical gateway RPC instead (AGENTS.md). This is a thin
client of it: `hermes_cli.gateway_client.connect_gateway` attaches to this HERMES_HOME's own gateway
(starting it when none runs) with the ticket from the profile's private control socket. The profile comes
from HERMES_HOME, never from a request. Talk adds no tool, approval path or policy:

- `session.create(run_budget=...)` opens a native session with Hermes' own per-run wall-clock budget, or
  `session.resume` continues the conversation's stored one. Its `toolsets` pick names `tasks.toolsets` plus every
  registered toolset contained in them (`launch_toolsets`): the gateway grants a composite such as
  `hermes-telegram` on its surface only when its default-off members are named too, and refuses the bare
  composite (`invalid_params`, field `toolsets`). The tools are exactly those of `tasks.toolsets`, never more.
- `prompt.submit(submission_id=<job id>, finite=True)` admits the request once. The job id is the
  idempotency key: the same submission returns the same admission, and the gateway refuses the id with
  other text (`admission_conflict`), so a restart never starts a second run. A finite turn is the core's
  native single-query context: nobody answers there, so flagged actions resolve through the profile's
  own `approvals.single_query_mode` (default deny) after its manual or smart policy. Talk never sets
  `unattended`, which on these cores is the `hermes -z` contract that auto-approves flagged actions,
  and readiness refuses a profile that would approve them.
- `prompt.receipt` is the durable admission state that a watcher, and a restarted Talk, follow.
- A submission whose answer was lost (a transport error, not a gateway refusal) is unknown, not refused:
  the job keeps its stored session and text and resubmits exactly them under the same id, which returns
  the admission the gateway took, if any. Only the gateway's own refusal (`AdmissionRefused`) fails it.
  The gateway admits a submit only from a connection attached to its session, so a reopened connection
  (after a drop or a Talk restart) first attaches with `session.resume`, as the gateway's own clients do.
- Stop is `prompt.cancel` while queued and `session.interrupt` once started. A job counts as stopped only
  when the receipt is terminal and the session no longer runs, never because a request was accepted or
  the client lost its connection. `stop` says whether its requests reached the gateway, so the watcher
  retries a persisted cancel or deadline after an outage.

A gateway error fails or delays the task; it never falls back to a local process.
"""
import asyncio
import contextlib
import logging
import threading
from pathlib import Path

from .agent import APPROVING, TASK_APPROVAL_MODES, Outcome, Readiness, _profile_config

logger = logging.getLogger(__name__)

TRANSPORT = "gateway"
RPC_TIMEOUT_S = 30.0
INTERRUPTED_OUTCOMES = frozenset({"cancelled", "interrupted", "terminated"})


class AdmissionRefused(RuntimeError):
    """The gateway itself answered that it did not take the submission (unlike a lost answer)."""
    refused = True


def client_available():
    """Whether this Hermes core ships the public gateway client (a capability, decided once at startup)."""
    try:
        from hermes_cli.gateway_client import connect_gateway  # noqa: F401
    except Exception:
        return False
    return True


def task_agent(tasks, home):
    """The transport this core supports: the gateway client when it ships one, else `hermes chat` processes.

    Chosen once from the installed core, never after an error: a gateway failure fails the task."""
    if client_available():
        return GatewayAgent(tasks, home)
    from .agent import NativeAgent
    return NativeAgent(tasks, home)


def readiness(tasks, home, available=None):
    """The gateway transport's readiness, with a non-sensitive reason when it is not ready."""
    if not tasks.enabled:
        return Readiness(False, "Tasks are off in the Talk config.")
    try:
        config = _profile_config(home)
    except Exception as exc:
        return Readiness(False, f"The profile config could not be read ({type(exc).__name__}).")
    approvals = config.get("approvals") or {}
    if not isinstance(approvals, dict):
        return Readiness(False, "The profile approvals setting is not a mapping. Talk refuses to start tasks.")
    mode = approvals.get("mode", "manual")
    if not isinstance(mode, str) or mode.strip().lower() not in TASK_APPROVAL_MODES:
        return Readiness(False, "The profile approval mode is not manual or smart, so a voice task could be "
                                "approved without the user. Talk refuses to start tasks.")
    for key in ("single_query_mode", "unattended_mode"):
        if str(approvals.get(key, "deny")).strip().lower() in APPROVING:
            return Readiness(False, "The profile auto-approves flagged actions in unattended runs. "
                                    "Talk refuses to start tasks.")
    model = config.get("model")
    if not (model.get("default") if isinstance(model, dict) else model):
        return Readiness(False, "The profile has no configured model for tasks.")
    try:
        import toolsets
        unknown = [name for name in tasks.toolsets if not toolsets.validate_toolset(name)]
    except Exception as exc:
        return Readiness(False, f"Hermes toolsets are unavailable ({type(exc).__name__}).")
    if unknown:
        return Readiness(False, f"Unknown Hermes toolsets in tasks.toolsets: {', '.join(unknown)}.")
    if not (client_available() if available is None else available):
        return Readiness(False, "This Hermes core has no gateway client.")
    return Readiness(True, "")


def receipt_outcome(receipt, session_id):
    """The job outcome a terminal admission receipt reports."""
    result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
    outcome = str(receipt.get("outcome") or "")
    text = result.get("final_response")
    text = text if isinstance(text, str) else ""
    if outcome == "completed" and result.get("completed", True) and not result.get("failed"):
        return Outcome(0, text, "", session_id)
    if outcome in INTERRUPTED_OUTCOMES or result.get("interrupted"):
        return Outcome(130, "", "The run was interrupted before it finished.", session_id)
    reason = result.get("error") or result.get("failure_reason") or f"Hermes reported the run {outcome or 'unknown'}."
    return Outcome(1, "", str(reason), session_id)


def launch_toolsets(names):
    """`session.create`'s toolsets for `names`: the names, then each registered toolset whose tools they cover.

    Adds no tool: every added toolset resolves to a subset of the tools `names` already resolve to."""
    import toolsets
    wanted = {tool for name in names for tool in toolsets.resolve_toolset(name)}
    members = [name for name in sorted(toolsets.get_all_toolsets()) if name not in names
               and (tools := set(toolsets.resolve_toolset(name))) and tools <= wanted]
    return list(names) + members


def _refusal_label(exc):
    """The gateway refusal's bounded reason code and field, for the log; never its detail or payload."""
    data = exc.data if isinstance(getattr(exc, "data", None), dict) else {}
    bounded = [v if isinstance(v, str) and v.replace("_", "").replace(".", "").isalnum() and len(v) <= 80 else "?"
               for v in (str(exc), data.get("field", ""))]
    return f"{bounded[0]}" + (f" (field {bounded[1]})" if bounded[1] else "")


class GatewayAgent:
    """One persistent public-client connection to the profile's gateway, driven from a private event loop."""

    transport = TRANSPORT

    def __init__(self, tasks, home, connect=None, available=None):
        self.tasks = tasks
        self.home = Path(home)
        self._connect = connect
        self._available = available
        self._lock = threading.Lock()
        self._loop = None
        self._client = None
        self._stack = None
        self._drain = None
        self._attached = set()  # sessions this connection is attached to (a reopened one is attached to none)
        self.finals = {}

    def readiness(self):
        return readiness(self.tasks, self.home, self._available)

    # -- transport -----------------------------------------------------------------------------------

    def _ensure_loop(self):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="talk-gateway-rpc", daemon=True).start()
            return self._loop

    async def _open(self):
        if self._client is not None:
            return self._client
        connect = self._connect
        if connect is None:
            from hermes_cli.gateway_client import connect_gateway as connect
        stack = contextlib.AsyncExitStack()
        try:
            client = await stack.enter_async_context(connect())
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self._client = stack, client
        self._drain = asyncio.create_task(self._discard_events(client))
        return client

    @staticmethod
    async def _discard_events(client):
        """Session events are not needed (state comes from receipts); consume them so the reader never stalls."""
        with contextlib.suppress(Exception, asyncio.CancelledError):
            while True:
                if isinstance(await client.events.get(), Exception):
                    return

    async def _close(self):
        client, stack, drain = self._client, self._stack, self._drain
        self._client = self._stack = self._drain = None
        self._attached = set()
        if drain is not None:
            drain.cancel()
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()
        return client

    async def _rpc(self, method, timeout, params):
        from hermes_cli.gateway_client import GatewayRPCError
        for attempt in (0, 1):
            client = await self._open()
            try:
                session_id = params.get("session_id")
                if method == "prompt.submit" and session_id not in self._attached:
                    # The gateway refuses a submit from a connection not attached to the session
                    # (`permission_denied`); attaching changes nothing in the session.
                    await client.rpc("session.resume", _timeout=timeout, session_id=session_id, omit_messages=True)
                    self._attached.add(session_id)
                result = await client.rpc(method, _timeout=timeout, **params)
                if method in ("session.create", "session.resume") and isinstance(result, dict):
                    self._attached.update(x for x in (session_id, result.get("session_id")) if x)
                return result
            except GatewayRPCError:
                raise  # the gateway's own answer
            except Exception:
                await self._close()  # a dead connection is reopened once; its outcome stays unknown
                if attempt:
                    raise

    def call(self, method, timeout=RPC_TIMEOUT_S, **params):
        future = asyncio.run_coroutine_threadsafe(self._rpc(method, timeout, params), self._ensure_loop())
        return future.result(timeout + 30)

    def close(self):
        if self._loop is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._close(), self._loop).result(15)

    # -- job operations --------------------------------------------------------------------------------

    def start(self, job_id, prompt, resume, persist):
        """Admit the job once. persist(**fields) stores the session before submission and the admission after."""
        if resume:
            snapshot = self.call("session.resume", session_id=resume, omit_messages=True)
        else:
            from hermes_cli.gateway_client import GatewayRPCError
            try:
                snapshot = self.call("session.create", cwd=str(self.tasks.workdir),
                                     toolsets=launch_toolsets(self.tasks.toolsets), max_turns=self.tasks.max_turns,
                                     run_budget=float(self.tasks.run_budget_s), timeout=60.0)
            except GatewayRPCError as exc:
                # The gateway's own refusal: no session exists and nothing ran.
                logger.warning("the Hermes gateway refused a task session: %s", _refusal_label(exc))
                raise AdmissionRefused(f"session.create refused: {_refusal_label(exc)}") from exc
        session_id = snapshot["session_id"]
        # Stored before submission: a restart resubmits exactly this text under the same submission id.
        persist(native_session_id=session_id, submission_text=prompt)
        return self.submit(job_id, session_id, prompt, persist)

    def submit(self, job_id, session_id, prompt, persist):
        """Raises AdmissionRefused on the gateway's own refusal; any other error leaves the admission unknown."""
        from hermes_cli.gateway_client import GatewayRPCError
        try:
            receipt = self.call("prompt.submit", session_id=session_id, text=prompt, submission_id=job_id,
                                finite=True)
        except GatewayRPCError as exc:
            raise AdmissionRefused(str(exc)[:200]) from exc
        if not isinstance(receipt, dict) or not receipt.get("admission_id"):
            raise AdmissionRefused("the gateway admitted nothing")
        persist(admission_id=receipt["admission_id"], submission_text=None)
        return receipt

    def settled(self, job):
        """True once the admission is terminal and its session runs nothing; False while it runs or is unknown."""
        if job.id in self.finals:
            return True
        try:
            receipt = self.call("prompt.receipt", session_id=job.native_session_id, admission_id=job.admission_id,
                                include_result=True)
        except Exception as exc:
            from hermes_cli.gateway_client import GatewayRPCError
            if isinstance(exc, GatewayRPCError) and exc.code is not None and "not found" in str(exc).lower():
                self.finals[job.id] = None  # the gateway no longer knows this admission: outcome unknown
                return True
            return False  # unreachable: still unknown, keep following
        if receipt.get("status") != "terminal":
            return False
        try:
            if self.call("session.resume", session_id=job.native_session_id, omit_messages=True).get("running"):
                return False
        except Exception:
            return False
        self.finals[job.id] = receipt
        return True

    def outcome(self, job):
        receipt = self.finals.pop(job.id, None)
        return None if receipt is None else receipt_outcome(receipt, job.native_session_id)

    def stop(self, job):
        """Ask the gateway to stop the job's run; True when the requests reached it, False to retry later.

        Whether it stopped is only what a later terminal receipt and idle session say."""
        if not (job.native_session_id and job.admission_id):
            return False
        try:
            receipt = self.call("prompt.receipt", session_id=job.native_session_id, admission_id=job.admission_id)
            if receipt.get("status") == "queued":
                self.call("prompt.cancel", session_id=job.native_session_id, admission_id=job.admission_id)
                receipt = self.call("prompt.receipt", session_id=job.native_session_id,
                                    admission_id=job.admission_id)
            if receipt.get("status") in ("started", "unknown") and receipt.get("execution_generation") is not None:
                self.call("session.interrupt", session_id=job.native_session_id,
                          execution_generation=receipt["execution_generation"])
        except Exception:
            return False  # unreachable or refused: the persisted stop intent is retried by the watcher
        return True
