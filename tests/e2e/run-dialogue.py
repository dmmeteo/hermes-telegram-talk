#!/usr/bin/env python3
"""Focused dialogue-lifecycle regressions. Synthetic data only; no network, no provider calls.

  python3 tests/e2e/run-dialogue.py

runs, against static/talk/app.js in this checkout:
  1. tests/dialogue/check.mjs: opening hold and single fallback, idle-gated speakable queue, commentary bypass,
     delegation correlation, exactly-once delivery and acks, reconnect cleanup, closure flush, UTF-8 chunking;
  2. tests/startup/lifecycle.mjs, tests/agent/delivery.mjs and tests/frontend/check.mjs (the existing harnesses);
  3. tests/dialogue/e2e.py: the real app.js against the real Talk server with a Ukrainian speak-first opening.
Node must be on PATH.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGES = [
    ("dialogue", ["node", "tests/dialogue/check.mjs"], r"dialogue checks passed"),
    ("lifecycle", ["node", "tests/startup/lifecycle.mjs", "static/talk/app.js"], None),
    ("delivery", ["node", "tests/agent/delivery.mjs", "static/talk/app.js"], r"delivery fault checks passed"),
    ("frontend", ["node", "tests/frontend/check.mjs"], r"Talk frontend checks passed"),
    ("e2e", [sys.executable, "tests/dialogue/e2e.py"], r"dialogue e2e checks passed"),
]


def main():
    if not shutil.which("node"):
        print("FAIL node is not on PATH")
        print("TALK_DIALOGUE_FAILED")
        return 1
    ok = True
    for name, command, _ in STAGES:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=600)
        print(f"== {name}: exit {result.returncode}")
        print((result.stdout + result.stderr).rstrip())
        ok = ok and result.returncode == 0
    print("TALK_DIALOGUE_" + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
