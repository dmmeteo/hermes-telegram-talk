#!/usr/bin/env python3
"""The canonical Hermes Telegram Talk gate for this checkout. Synthetic data only; no network, no provider calls.

  python3 verify-integrated.py --offline [--require-host]
      verify-backend.py --offline: legacy signaling suite, Talk checks on in-memory history, and the same
          checks plus plugin registration and `hermes talk serve` under a real Hermes runtime in disposable
          homes (see its docstring for how the runtime is found);
      verify-frontend.py --offline: frontend unit checks and the HTTP contract against this backend;
      verify-pulse.py --offline: per-item Pulse acceptance through the real routes, in memory and on SessionDB;
      verify-pulse-engine.py --offline: model selection, memory extraction and follow-up checks through the
          plugin's host ctx.llm against a deterministic local model, the real routes and SessionDB (needs a
          Hermes runtime, so it always counts as a host stage).
      --require-host fails instead of skipping when no Hermes runtime is found.
  python3 verify-integrated.py --browser OUT_DIR
      the legacy Chromium inventory (verify.py --browser) and the Talk Chromium inventory
      (verify-frontend.py --browser), each in a capped network-less container.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
COUNTS = {"legacy": r"(\d+)/(\d+) offline checks passed", "memory": r"(\d+)/(\d+) talk checks passed \[memory\]",
          "host": r"(\d+)/(\d+) talk checks passed \[host\]", "frontend": r"(\d+)/(\d+) Talk frontend checks passed",
          "http": r"Ran (\d+) tests?()", "pulse_memory": r"(\d+)/(\d+) pulse checks passed \[memory\]",
          "pulse_host": r"(\d+)/(\d+) pulse checks passed \[host\]",
          "pulse_engine": r"(\d+)/(\d+) engine steps passed \[host\]"}


def stage(name, cmd, env=None):
    result = subprocess.run(cmd, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})},
                            capture_output=True, text=True, timeout=1800)
    output = result.stdout + result.stderr
    print(f"== {name}: exit {result.returncode}")
    print("\n".join(line for line in output.splitlines() if not line.startswith("PASS ")))
    return result.returncode, output


def counted(output, keys):
    found = {}
    for key in keys:
        match = re.findall(COUNTS[key], output)
        if match:
            passed, total = match[-1]
            found[key] = f"{passed}/{total or passed}"
    return found


def offline(require_host):
    code, backend = stage("backend", [sys.executable, "verify-backend.py", "--offline"],
                          {"HERMES_TALK_REQUIRE_HOST": "1"} if require_host else None)
    ok = code == 0 and "BACKEND OK" in backend
    code, frontend = stage("frontend", [sys.executable, "verify-frontend.py", "--offline"])
    ok = ok and code == 0 and "TALK_FRONTEND_OFFLINE_OK" in frontend and "\nOK" in frontend
    code, pulse = stage("pulse", [sys.executable, "verify-pulse.py", "--offline"],
                        {"HERMES_TALK_REQUIRE_HOST": "1"} if require_host else None)
    ok = ok and code == 0 and "TALK_PULSE_OK" in pulse
    code, engine = stage("pulse-engine", [sys.executable, "verify-pulse-engine.py", "--offline"])
    ok = ok and code == 0 and "TALK_PULSE_ENGINE_PARITY_OK" in engine
    counts = {**counted(backend, ("legacy", "memory", "host")), **counted(frontend, ("frontend", "http")),
              **counted(pulse, ("pulse_memory", "pulse_host")), **counted(engine, ("pulse_engine",))}
    if "host" not in counts or "pulse_host" not in counts:
        ok = ok and not require_host
    print("counts", json.dumps(counts, sort_keys=True))
    print("TALK_INTEGRATED_OFFLINE_" + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def browser(out_dir):
    out = Path(out_dir).resolve()
    legacy, _ = stage("legacy browser", [sys.executable, "verify.py", "--browser", str(out / "legacy")])
    talk, _ = stage("talk browser", [sys.executable, "verify-frontend.py", "--browser", str(out / "talk")])
    summaries = {}
    for name in ("legacy", "talk"):
        log = out / name / "browser.log"
        lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.is_file() else []
        summaries[name] = next((line for line in reversed(lines) if line.get("step") == "summary"), None)
    print("summaries", json.dumps(summaries, sort_keys=True))
    ok = legacy == 0 and talk == 0
    print("TALK_INTEGRATED_BROWSER_" + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--browser", metavar="OUT_DIR")
    parser.add_argument("--require-host", action="store_true")
    args = parser.parse_args()
    return offline(args.require_host) if args.offline else browser(args.browser)


if __name__ == "__main__":
    sys.exit(main())
