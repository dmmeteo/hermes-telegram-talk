"""A bounded stand-in for `hermes chat` in the work gate. No model, provider, Hermes core or T3.

Talk starts it exactly as it starts Hermes (agent.NativeAgent behind spool.py, its own process group, with
HERMES_TALK_JOB). Where a real turn would call t3_control, it runs the post_tool_call hook body (work.observe) with
what the core passes it, against the synthetic T3 store in $TALK_WORK_DIR/t3: the result T3 would give, or, for a
call the core gave up on, the core's own made-up result and status (agent/tool_executor.py: `cancelled` with the
call's arguments when a stop abandons a running call, `timeout` at the tool deadline, `blocked` for a call a hook
refused, and `cancelled` with empty arguments for a call it never started). The real core's paths themselves are
driven by tests/e2e/run-agent.py (t3_cut_call_real_core). TASK words:

  hold K                 a micro request: hold until release/K
  code K                 t3 start of thread th-K, then end the turn
  codewait K             t3 start of thread th-K, then hold until release/K (a turn that waits on T3)
  send THREAD K          t3 send to THREAD, then end the turn
  lost K / lostnone K    a t3 start whose answer was lost; T3 has the thread / does not
  lostnothread K         a t3 start whose answer was lost before any thread id came back
  refused K              a t3 start T3 refused
  look THREAD K          a t3 read of THREAD (a status read), then end the turn
  ... intent K           (the request's last two words, after any speech) the admission scripted in
                         $TALK_WORK_DIR/intents/K.json: {"action": "start"|"send", "thread", "title"?, "message"}
  cutstart K             T3 accepts a start of th-K; the turn is stopped inside the call (hook: cancelled)
  cutsend THREAD K       T3 accepts a send to THREAD; the turn is stopped inside the call (hook: cancelled)
  timeout K              T3 accepts a start of th-K after the core's tool deadline (hook: timeout); the turn goes on
  retry K                as timeout, then the same call again, which T3 answers with the start it already took
  blocked K              a t3 start a hook refused, so it never reached T3 (hook: blocked); the turn ends itself
  skipped K              a t3 start the core never started (hook: cancelled, no arguments); the turn ends itself
"""
import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hermes_talk import work  # noqa: E402
from fake_t3 import FakeT3  # noqa: E402

HOLD_MAX_S = 120

root = Path(os.environ["TALK_WORK_DIR"])
home = os.environ["HERMES_HOME"]
t3 = FakeT3(root / "t3")
args = sys.argv[1:]
resume = args[args.index("--resume") + 1] if "--resume" in args else ""
prompt = sys.stdin.read()
words = [line[5:].strip() for line in prompt.splitlines() if line.startswith("TASK:")][-1].split()
verb, key = words[0], words[-1]
if len(words) >= 2 and words[-2] == "intent":
    verb = "intent"
session = resume or "syn_" + secrets.token_hex(6)


def emit(event):
    print(json.dumps(event), flush=True)


def log(kind, **extra):
    with open(root / "ledger.jsonl", "a") as handle:
        handle.write(json.dumps({"event": kind, "key": key, "session": session, "resume": resume, "pid": os.getpid(),
                                 "job": os.environ.get("HERMES_TALK_JOB"), "at": time.time(), **extra}) + "\n")


def tool(action, result, status=None, **call):
    """One t3_control call as the post hook sees it: T3's answer, or the core's own result and status."""
    call = {"action": action, "request_id": f"req-{key}-{action}", **call} if action else {}
    work.observe(tool_name="t3_control", args=call, result=result if isinstance(result, str) else json.dumps(result),
                 session_id=session, home=home, status=status or ("ok" if result.get("ok") else "error"))


def abandoned(action, **call):
    """The core's report of a running call it stopped waiting for after a stop (its real answer is dropped)."""
    tool(action, "[Tool execution cancelled — t3_control was abandoned: interrupt requested]", "cancelled", **call)


def stop(*_):
    raise KeyboardInterrupt


def hold():
    deadline = time.monotonic() + HOLD_MAX_S
    while not (root / "release" / key).exists():
        if time.monotonic() > deadline:
            raise KeyboardInterrupt
        time.sleep(0.05)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
emit({"type": "system", "session_id": session})
log("start", verb=verb)
cut = None
try:
    text = f"done {key}"
    if verb in ("code", "codewait"):
        tool("start", {"ok": True, "action": "start", **t3.start(f"th-{key}")}, cwd="/synthetic", message=key)
        log("t3", thread=f"th-{key}")
        text = f"Started T3 thread th-{key}."
    elif verb == "send":
        tool("send", {"ok": True, "action": "send", **t3.send(words[1])}, thread_id=words[1], message=key)
        log("t3", thread=words[1])
        text = f"Sent to {words[1]}."
    elif verb == "intent":
        spec = json.loads((root / "intents" / f"{key}.json").read_text())
        extra = {k: spec[k] for k in ("title",) if k in spec}
        if spec["action"] == "start":
            tool("start", {"ok": True, "action": "start", **t3.start(spec["thread"])}, cwd="/synthetic",
                 message=spec["message"], **extra)
        else:
            tool("send", {"ok": True, "action": "send", **t3.send(spec["thread"])}, thread_id=spec["thread"],
                 message=spec["message"])
        log("t3", thread=spec["thread"])
        text = f"Admitted T3 work on {spec['thread']}."
    elif verb == "lost":
        t3.start(f"th-{key}")
        tool("start", {"ok": False, "error": "transport_error", "outcome_unknown": True, "thread_id": f"th-{key}"},
             cwd="/synthetic", message=key)
    elif verb == "lostnone":
        tool("start", {"ok": False, "error": "transport_error", "outcome_unknown": True, "thread_id": f"th-{key}"},
             cwd="/synthetic", message=key)
    elif verb == "lostnothread":
        tool("start", {"ok": False, "error": "transport_error", "outcome_unknown": True, "committed_calls": 0},
             cwd="/synthetic", message=key)
    elif verb == "refused":
        tool("start", {"ok": False, "error": "rejected", "outcome_unknown": False}, cwd="/synthetic", message=key)
    elif verb == "look":
        tool("read", {"ok": True, "action": "read", "state": t3.state(words[1])}, thread_id=words[1])
    elif verb == "cutstart":
        cut = ("start", {"cwd": "/synthetic", "message": key})
        t3.start(f"th-{key}")
        log("t3", thread=f"th-{key}")
        hold()
    elif verb == "cutsend":
        cut = ("send", {"thread_id": words[1], "message": key})
        t3.send(words[1])
        log("t3", thread=words[1])
        hold()
    elif verb in ("timeout", "retry"):
        tool("start", "Error executing tool 't3_control': timed out after 2.0s", "timeout", cwd="/synthetic",
             message=key)
        started = {"ok": True, "action": "start", **t3.start(f"th-{key}")}
        log("t3", thread=f"th-{key}")
        if verb == "retry":
            tool("start", started, cwd="/synthetic", message=key)
    elif verb == "blocked":
        tool("start", {"error": "blocked by a hook", "message": "blocked by a hook"}, "blocked", cwd="/synthetic",
             message=key)
    elif verb == "skipped":
        tool(None, "[Tool execution cancelled — t3_control was skipped. interrupt requested]", "cancelled")
    if verb in ("hold", "codewait"):
        hold()
    log("end")
    emit({"type": "result", "exit_code": 0, "text": text, "session_id": session})
except KeyboardInterrupt:
    if cut:
        abandoned(cut[0], **cut[1])
    log("stop")
    emit({"type": "result", "exit_code": 130, "text": "", "error": "interrupted", "session_id": session})
    sys.exit(130)
