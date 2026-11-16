"""A bounded stand-in for `hermes chat` in the work gate. No model, provider, Hermes core or T3.

Talk starts it exactly as it starts Hermes (agent.NativeAgent behind spool.py, its own process group, with
HERMES_TALK_JOB). Where a real turn would call t3_control, it runs the pre_tool_call hook body (work.intend), then
the post_tool_call hook body (work.observe) with the result T3 would give, against the synthetic T3 store in
$TALK_WORK_DIR/t3. TASK words:

  hold K                 a micro request: hold until release/K
  code K                 t3 start of thread th-K, then end the turn
  codewait K             t3 start of thread th-K, then hold until release/K (a turn that waits on T3)
  send THREAD K          t3 send to THREAD, then end the turn
  lost K / lostnone K    a t3 start whose answer was lost; T3 has the thread / does not
  lostnothread K         a t3 start whose answer was lost before any thread id came back
  refused K              a t3 start T3 refused
  look THREAD K          a t3 read of THREAD (a status read), then end the turn
  cutstart K             T3 accepts a start of th-K, but the turn is stopped before the post hook records it
  cutsend THREAD K       T3 accepts a send to THREAD, but the turn is stopped before the post hook records it
  blocked K              a t3 start that never reached T3 and got no post hook (blocked), then the turn ends itself
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
session = resume or "syn_" + secrets.token_hex(6)


def emit(event):
    print(json.dumps(event), flush=True)


def log(kind, **extra):
    with open(root / "ledger.jsonl", "a") as handle:
        handle.write(json.dumps({"event": kind, "key": key, "session": session, "resume": resume, "pid": os.getpid(),
                                 "job": os.environ.get("HERMES_TALK_JOB"), "at": time.time(), **extra}) + "\n")


def tool(action, result, **call):
    """One t3_control call as the hooks see it; result None is a call whose post hook never ran."""
    call = {"action": action, "request_id": f"req-{key}-{action}", **call}
    work.intend(tool_name="t3_control", args=call, session_id=session, home=home)
    if result is not None:
        work.observe(tool_name="t3_control", args=call, result=json.dumps(result), session_id=session, home=home)


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
        tool("start", None, cwd="/synthetic", message=key)
        t3.start(f"th-{key}")
        log("t3", thread=f"th-{key}")
    elif verb == "cutsend":
        tool("send", None, thread_id=words[1], message=key)
        t3.send(words[1])
        log("t3", thread=words[1])
    elif verb == "blocked":
        tool("start", None, cwd="/synthetic", message=key)
    if verb in ("hold", "codewait", "cutstart", "cutsend"):
        hold()
    log("end")
    emit({"type": "result", "exit_code": 0, "text": text, "session_id": session})
except KeyboardInterrupt:
    log("stop")
    emit({"type": "result", "exit_code": 130, "text": "", "error": "interrupted", "session_id": session})
    sys.exit(130)
