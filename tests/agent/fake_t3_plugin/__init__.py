"""Synthetic `t3_control` for run-agent: the real t3-bridge's result shapes over a file store, no T3 server.

install(home, root, store) copies this plugin into a synthetic profile with the repository root and store folder."""
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "store.json"


def install(home, root, store):
    target = Path(home) / "plugins" / "t3-bridge"
    shutil.copytree(HERE, target, ignore=shutil.ignore_patterns("__pycache__", "store.json"))
    (target / "store.json").write_text(json.dumps({"root": str(root), "store": str(store)}))
    return target


def register(ctx):
    paths = json.loads(CONFIG.read_text())
    if paths["root"] not in sys.path:
        sys.path.insert(0, paths["root"])
    from tests.work.fake_t3 import FakeT3
    store = FakeT3(paths["store"])

    def handle(args, **_):
        action = args.get("action")
        if action == "start":
            return json.dumps({"ok": True, "action": "start", **store.start("th-" + str(args.get("message")))})
        if action == "send":
            return json.dumps({"ok": True, "action": "send", **store.send(str(args.get("thread_id")))})
        return store.dispatch(args)

    schema = {"name": "t3_control", "description": "Synthetic T3 threads for offline checks.",
              "parameters": {"type": "object", "properties": {
                  "action": {"type": "string"}, "thread_id": {"type": "string"}, "message": {"type": "string"},
                  "cwd": {"type": "string"}, "request_id": {"type": "string"}}, "required": ["action"]}}
    ctx.register_tool(name="t3_control", toolset="t3_bridge", schema=schema, handler=handle,
                      description=schema["description"], emoji="🧵")
