"""Which voice tasks get a Telegram status message, per account, as two independent switches.

`large` is a task T3 admitted work from (on by default); `micro` is every other task (off by default). The profile's
Talk config sets the defaults (receipts.large_work, receipts.micro_tasks); a task user changes their own switches
with `/talk_notify` in their private chat with the bot (thread_gateway.py). The choice is stored per (profile, bot,
user) in the profile's plugin data, which the gateway writes and the voice server reads.

A switch decides only whether a task that has no status message yet gets one. A message that already exists keeps
following its task to the final status. When a switch turns on, a task still open gets its message at its next
status change; a finished task is never posted.
"""
import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

LARGE, MICRO = "large", "micro"
KINDS = (LARGE, MICRO)
LABELS = {LARGE: "T3 code work", MICRO: "Quick requests"}
FILE = ("plugin-data", "hermes-talk", "notify.json")


@dataclass(frozen=True)
class Visibility:
    large: bool = True
    micro: bool = False

    def shows(self, kind):
        return bool(getattr(self, kind))

    def public(self):
        return {LARGE: self.large, MICRO: self.micro}


def _path(home):
    return Path(home).joinpath(*FILE)


def _read(home):
    try:
        data = json.loads(_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def _locked(home):
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_suffix(".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load(home, scope_key, default):
    """The account's switches: each one it set, else the configured default."""
    stored = _read(home).get(scope_key) if home is not None else None
    if not isinstance(stored, dict):
        return default
    return Visibility(**{k: stored[k] if type(stored.get(k)) is bool else getattr(default, k) for k in KINDS})


def toggle(home, scope_key, kind, default, clock=time.time):
    """Flip one switch of one account and store both; returns the new switches."""
    if kind not in KINDS:
        raise ValueError(kind)
    with _locked(home):
        data = _read(home)
        current = load(home, scope_key, default)
        new = replace(current, **{kind: not current.shows(kind)})
        data[scope_key] = {**new.public(), "at": clock()}
        path = _path(home)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return new


def panel(visibility):
    """(text, [(label, kind)]): the English message and the two buttons `/talk_notify` shows."""
    text = ("Telegram status messages for your voice tasks. Tap a button to switch it. Messages that already "
            "exist still follow their task to the end; earlier tasks are not posted.")
    return text, [(f"{LABELS[k]}: {'on' if visibility.shows(k) else 'off'}", k) for k in KINDS]
