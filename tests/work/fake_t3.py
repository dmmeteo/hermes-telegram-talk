"""A synthetic T3 thread store for the work gate: one JSON file per thread, shaped like t3_control's results.

The check drives each thread's outcome (running, blocked, completed, failed, interrupted) by writing its file; the
runner admits starts and sends into it; Talk reads and interrupts it through `dispatch`, the stand-in for the
profile's t3_control handler. Nothing here talks to a real T3 server.
"""
import json
import os
from pathlib import Path


class FakeT3:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, thread_id):
        return self.root / f"{thread_id}.json"

    def get(self, thread_id):
        try:
            return json.loads(self._path(thread_id).read_text())
        except (OSError, ValueError):
            return None

    def put(self, thread_id, **changes):
        data = {**(self.get(thread_id) or {"outcome": "idle", "count": 0, "messages": [], "interrupts": 0}),
                **changes}
        tmp = self._path(thread_id).with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self._path(thread_id))
        return data

    def state(self, thread_id):
        data = self.get(thread_id) or {}
        return {"outcome": data.get("outcome"), "run": {"count": data.get("count"), "active_id": None}}

    def start(self, thread_id):
        if self.get(thread_id) is None:
            self.put(thread_id, outcome="running", count=1)
        return {"thread_id": thread_id, "state": self.state(thread_id)}

    def send(self, thread_id):
        data = self.get(thread_id)
        delivery = "queued" if data and data["outcome"] == "running" else "started"
        if delivery == "started":
            self.put(thread_id, outcome="running", count=(data or {}).get("count", 0) + 1)
        else:
            self.put(thread_id, waiting=(data.get("waiting") or 0) + 1)
        return {"thread_id": thread_id, "delivery": delivery, "state": self.state(thread_id)}

    def finish(self, thread_id, outcome, text=""):
        data = self.get(thread_id)
        messages = data["messages"] + ([{"role": "assistant", "text": text}] if text else [])
        if data.get("waiting"):  # a queued message starts the next run once this one ends
            return self.put(thread_id, outcome="running", count=data["count"] + 1, waiting=data["waiting"] - 1,
                            messages=messages)
        return self.put(thread_id, outcome=outcome, messages=messages)

    def dispatch(self, args):
        thread_id = args.get("thread_id")
        data = self.get(thread_id)
        if data is None:
            return json.dumps({"ok": False, "error": "not_found", "message": "no such thread"})
        if args["action"] == "read":
            reads = self.put(thread_id, reads=data.get("reads", 0) + 1)
            return json.dumps({"ok": True, "action": "read", "state": self.state(thread_id),
                               "messages": reads["messages"][-args.get("messages", 2):]})
        if args["action"] == "interrupt":
            self.put(thread_id, interrupts=data.get("interrupts", 0) + 1)
            if data["outcome"] in ("running", "blocked"):
                self.put(thread_id, outcome="interrupted", waiting=0)
                return json.dumps({"ok": True, "action": "interrupt", "interrupted": True})
            return json.dumps({"ok": True, "action": "interrupt", "interrupted": False})
        return json.dumps({"ok": False, "error": "invalid_input"})
