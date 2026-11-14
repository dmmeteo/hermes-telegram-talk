"""A bounded stand-in for `hermes chat` in the capacity gate. No model, provider or Hermes core.

Talk starts it exactly as it starts Hermes (agent.NativeAgent behind spool.py, its own process group). It reads
the prompt, reports its native session (the `--resume` one, else a fresh id) as Hermes' stream-json does, logs
start/end/stop to $TALK_CAPACITY_DIR/ledger.jsonl, and holds until the file release/<last word of TASK>
exists. SIGINT or SIGTERM ends it as interrupted (exit 130), the way Hermes reports a stop.
"""
import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path

HOLD_MAX_S = 120

root = Path(os.environ["TALK_CAPACITY_DIR"])
args = sys.argv[1:]
resume = args[args.index("--resume") + 1] if "--resume" in args else ""
prompt = sys.stdin.read()
task = [line[5:].strip() for line in prompt.splitlines() if line.startswith("TASK:")][-1]
key = task.split()[-1]
session = resume or "syn_" + secrets.token_hex(6)


def emit(event):
    print(json.dumps(event), flush=True)


def log(kind):
    with open(root / "ledger.jsonl", "a") as handle:
        handle.write(json.dumps({"event": kind, "key": key, "session": session, "resume": resume,
                                 "pid": os.getpid(), "at": time.time()}) + "\n")


def stop(*_):
    raise KeyboardInterrupt


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
emit({"type": "system", "session_id": session})
log("start")
try:
    deadline = time.monotonic() + HOLD_MAX_S
    while not (root / "release" / key).exists():
        if time.monotonic() > deadline:
            raise KeyboardInterrupt
        time.sleep(0.05)
    log("end")
    emit({"type": "result", "exit_code": 0, "text": f"done {key}", "session_id": session})
except KeyboardInterrupt:
    log("stop")
    emit({"type": "result", "exit_code": 130, "text": "", "error": "interrupted", "session_id": session})
    sys.exit(130)
