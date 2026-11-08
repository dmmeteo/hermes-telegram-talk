"""Runs voice tasks as native Hermes turns: one `hermes chat` single-query process per job.

The process is the profile's own CLI agent under the same HERMES_HOME, so it loads that profile's
model, provider, toolsets, skills, memory and approval policy itself. Talk adds no tool and no
approval path. Single-query mode has nobody to answer an approval, and Hermes blocks flagged actions
there unless the profile set approvals.single_query_mode: approve; readiness() refuses that and any
other bypass. Hermes resolves that single-query deny before its smart classifier, so approvals.mode
manual and smart deny flagged actions alike here; off and unrecognised modes are refused. Output goes
to a per-job file, so a job keeps running across a Talk restart and is adopted, or reconciled from
its file, afterwards.
"""
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SOURCE = "telegram-voice"
DATA_DIR = ("plugin-data", "hermes-talk", "jobs")
CHILD_ENV_DROP = ("TG_VOICE_BOT_TOKEN", "TG_VOICE_ALLOWED_USER_IDS", "HERMES_YOLO_MODE", "HERMES_ACCEPT_HOOKS",
                  "HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION", "HERMES_SESSION_PLATFORM", "HERMES_CRON_SESSION",
                  "HERMES_INTERACTIVE", "HERMES_SESSION_SOURCE")
APPROVING = {"approve", "off", "allow", "yes", "true"}
TASK_APPROVAL_MODES = {"manual", "smart"}
CANCEL_GRACE_S = 8.0
# Above the largest line spool.py writes (1 MiB of text, at most 6 bytes per character once JSON-escaped).
OUTPUT_MAX_BYTES = 8 * 1024 * 1024
SPOOL = Path(__file__).resolve().parent / "spool.py"


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    text: str
    error: str
    session_id: str
    text_cut: int = 0
    oversized: int = 0


def hermes_command():
    exe = Path(sys.executable).parent / "hermes"
    return [str(exe)] if exe.is_file() else [sys.executable, "-m", "hermes_cli.main"]


def _profile_config(home):
    path = Path(home) / "config.yaml"
    if not path.is_file():
        return {}
    from .config import _yaml_load
    data = _yaml_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def readiness(tasks, home, command=None, env=None):
    """Whether this profile can run voice tasks now, with a non-sensitive reason when it cannot."""
    if not tasks.enabled:
        return Readiness(False, "Tasks are off in the Talk config.")
    env = os.environ if env is None else env
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
    if str(approvals.get("single_query_mode", "deny")).strip().lower() in APPROVING:
        return Readiness(False, "The profile auto-approves flagged actions in single-query runs. "
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
    head = Path((command or hermes_command())[0])
    if not head.is_file():
        return Readiness(False, "The Hermes command is not installed next to this interpreter.")
    return Readiness(True, "")


def process_start(pid):
    """The kernel start time of pid, which tells a live job process from a reused pid."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]


def alive(pid, start):
    return bool(pid) and start is not None and process_start(pid) == start


def read_outcome(path):
    """The final result event of a job's output record, or None if the run never reported one.

    The record is the bounded file spool.py writes; a raw stream-json file from an earlier Talk
    version is read the same way, line by line.
    """
    session_id, result, oversized = "", None, 0
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                if len(raw) > OUTPUT_MAX_BYTES or not raw.startswith(b"{"):
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("type") == "system" and event.get("session_id"):
                    session_id = str(event["session_id"])
                elif event.get("type") == "result":
                    result = event
                elif event.get("type") == "spool":
                    oversized = int(event.get("oversized") or 0)
    except OSError:
        return None
    if result is None:
        return Outcome(1, "", f"The run ended without a readable final result; {oversized} output line(s) exceeded "
                              "the spool limit.", session_id, 0, oversized) if oversized else None
    code = result.get("exit_code")
    return Outcome(exit_code=code if type(code) is int else 1, text=str(result.get("text") or ""),
                   error=str(result.get("error") or ""), session_id=str(result.get("session_id") or session_id),
                   text_cut=int(result.get("text_cut") or 0), oversized=oversized)


class NativeAgent:
    def __init__(self, tasks, home, command=None, env=None):
        self.tasks = tasks
        self.home = Path(home)
        self.command = command or hermes_command()
        base = dict(os.environ if env is None else env)
        self.env = {k: v for k, v in base.items() if k not in CHILD_ENV_DROP}
        self.env["HERMES_HOME"] = str(self.home)
        self.dir = self.home.joinpath(*DATA_DIR)

    def readiness(self):
        return readiness(self.tasks, self.home, self.command, self.env)

    def argv(self, resume):
        argv = [*self.command, "chat", "--query-file", "-", "--format", "stream-json", "--source", SOURCE,
                "--max-turns", str(self.tasks.max_turns), "--run-budget", str(self.tasks.run_budget_s),
                "-t", ",".join(self.tasks.toolsets)]
        return argv + (["--resume", resume] if resume else [])

    def start(self, job_id, prompt, resume):
        """Start the native run behind its spool; returns (process, output path). The caller persists the pid."""
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        output = self.dir / f"{job_id}.jsonl"
        data = prompt.encode("utf-8", "replace")
        process = subprocess.Popen([sys.executable, str(SPOOL), str(output), "--", *self.argv(resume)],
                                   cwd=str(self.tasks.workdir), env=self.env, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            process.stdin.write(data)
        except OSError:
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
        return process, str(output)

    @staticmethod
    def signal(pid, start, sig):
        if alive(pid, start):
            try:
                os.killpg(pid, sig)
            except OSError:
                pass

    def stop(self, pid, start, wait=None):
        """Interrupt the run (Hermes reports it as interrupted), escalating if it does not exit."""
        self.signal(pid, start, signal.SIGINT)
        for sig, grace in ((signal.SIGTERM, CANCEL_GRACE_S), (signal.SIGKILL, CANCEL_GRACE_S)):
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if wait and wait(0.2) is not None or not alive(pid, start):
                    return
                time.sleep(0.2)
            self.signal(pid, start, sig)

    def discard_output(self, path):
        try:
            Path(path).unlink()
        except (OSError, TypeError):
            pass
