"""A synthetic T3 thread store for the work gate: one JSON file per thread, shaped like t3_control's results.

The check drives each thread's outcome (running, blocked, completed, failed, interrupted) by writing its file; the
runner admits starts and sends into it; Talk reads and interrupts it through `dispatch`, the stand-in for the
profile's t3_control handler. Nothing here talks to a real T3 server.
"""
import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path


class FakeT3:
    """Every read-modify-write holds one file lock: the check, the runner processes and Talk's follower share it."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self):
        with open(self.root / ".lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _path(self, thread_id):
        return self.root / f"{thread_id}.json"

    def get(self, thread_id):
        try:
            return json.loads(self._path(thread_id).read_text())
        except (OSError, ValueError):
            return None

    def _write(self, thread_id, data):
        runs = list(data.get("runs") or [])
        while len(runs) < data["count"]:  # every counted run has an id, as T3's thread show reports them
            runs.append(f"run-{thread_id}-{len(runs) + 1}")
        data["runs"] = runs
        tmp = self._path(thread_id).with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self._path(thread_id))
        return data

    def update(self, thread_id, change):
        """change(current or None) -> new fields, applied under the lock; returns the stored thread."""
        with self._locked():
            current = self.get(thread_id)
            fields = change(current)
            base = current or {"outcome": "idle", "count": 0, "messages": [], "interrupts": 0, "runs": []}
            return self._write(thread_id, {**base, **fields})

    def put(self, thread_id, **changes):
        return self.update(thread_id, lambda current: changes)

    def state(self, thread_id, data=None):
        data = data or self.get(thread_id) or {}
        runs = data.get("runs") or []
        live = data.get("outcome") in ("running", "blocked")
        run = {"count": data.get("count"), "active_id": runs[-1] if live and runs else None,
               "latest_id": runs[-1] if runs else None}
        if data.get("no_ids"):  # a T3 that reports no run ids: Talk falls back to the run count
            run = {"count": data.get("count"), "active_id": None, "latest_id": None}
        return {"outcome": data.get("outcome"), "run": run}

    def start(self, thread_id):
        data = self.update(thread_id, lambda cur: {} if cur else {"outcome": "running", "count": 1})
        return {"thread_id": thread_id, "state": self.state(thread_id, data)}

    def send(self, thread_id):
        out = {}

        def change(cur):
            out["delivery"] = "queued" if cur and cur["outcome"] == "running" else "started"
            if out["delivery"] == "started":
                return {"outcome": "running", "count": (cur or {}).get("count", 0) + 1}
            return {"waiting": (cur.get("waiting") or 0) + 1}
        data = self.update(thread_id, change)
        return {"thread_id": thread_id, "delivery": out["delivery"], "state": self.state(thread_id, data)}

    def finish(self, thread_id, outcome, text=""):
        def change(cur):
            messages = cur["messages"] + ([{"role": "assistant", "text": text}] if text else [])
            if cur.get("waiting"):  # a queued message starts the next run once this one ends
                return {"outcome": "running", "count": cur["count"] + 1, "waiting": cur["waiting"] - 1,
                        "messages": messages}
            return {"outcome": outcome, "messages": messages}
        return self.update(thread_id, change)

    def dispatch(self, args):
        thread_id = args.get("thread_id")
        if self.get(thread_id) is None:
            return json.dumps({"ok": False, "error": "not_found", "message": "no such thread"})
        if args["action"] == "read":
            data = self.update(thread_id, lambda cur: {"reads": (cur or {}).get("reads", 0) + 1})
            return json.dumps({"ok": True, "action": "read", "state": self.state(thread_id, data),
                               "messages": data["messages"][-args.get("messages", 2):]})
        if args["action"] == "interrupt":
            out = {}

            def change(cur):
                out["hit"] = cur["outcome"] in ("running", "blocked")
                fields = {"interrupts": cur.get("interrupts", 0) + 1}
                return {**fields, "outcome": "interrupted", "waiting": 0} if out["hit"] else fields
            self.update(thread_id, change)
            return json.dumps({"ok": True, "action": "interrupt", "interrupted": out["hit"]})
        return json.dumps({"ok": False, "error": "invalid_input"})
