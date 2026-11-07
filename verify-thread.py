#!/usr/bin/env python3
"""Exact-thread Mini App inventory: chat input of the initiating Telegram thread, reply voice, Shopy persona.

  python3 verify-thread.py --offline [--require-host]

Stage 1 runs tests/thread/checks.py on the in-memory history with stand-in events through the hook body.
Stage 2 runs the same checks under a real Hermes runtime (HERMES_TALK_HERMES_PYTHON, else the first installed
one) in a disposable home: real SessionDB, the plugin loaded by Hermes' PluginManager, events built by the real
Telegram adapter from PTB updates (media through Hermes' own cache functions), dispatched through the gateway's
real `_hm_pre_gateway_dispatch_hook`. It prints TALK_THREAD_OK only when every check of both stages passed.
"""
import os
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "thread"))


def stand_in_event(text="", message_id=1, user_id=None, chat_id=None, chat_type="dm", thread_id=None,
                   bot_id="123456", internal=False, platform="telegram", message_type="text", media=(), caption=None,
                   edit_revision=None):
    import checks
    from hermes_talk import fixtures as fx
    user_id = str(fx.USER_ID) if user_id is None else user_id
    chat_id = user_id if chat_id is None else chat_id
    bot = types.SimpleNamespace(id=int(bot_id)) if bot_id else None
    raw = types.SimpleNamespace(caption=caption, get_bot=(lambda: bot) if bot else (lambda: (_ for _ in ()).throw(
        RuntimeError("no bot"))))
    del checks
    return types.SimpleNamespace(
        text=text, message_type=types.SimpleNamespace(value=message_type), message_id=str(message_id),
        internal=internal, raw_message=raw, edit_revision=edit_revision,
        media_urls=[m[0] for m in media], media_types=[m[1] for m in media],
        source=types.SimpleNamespace(platform=types.SimpleNamespace(value=platform), chat_id=chat_id,
                                     chat_type=chat_type, user_id=user_id, thread_id=thread_id, is_bot=False))


def stage_memory():
    import checks
    from hermes_talk.history import MemoryHistory
    from hermes_talk.thread_inbox import take_event
    results = checks.run(lambda home: MemoryHistory(), stand_in_event, lambda home, event: take_event(event, home),
                         print)
    return all(ok for _, ok, _ in results), len(results)


def hermes_python():
    explicit = os.environ.get("HERMES_TALK_HERMES_PYTHON")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    found = sorted(Path.home().glob(".hermes/installs/*/environments/*/venv/bin/python"))
    return str(found[0]) if found else None


def main():
    sys.stdout.reconfigure(line_buffering=True)
    if "--offline" not in sys.argv[1:]:
        print(__doc__)
        return 2
    require_host = "--require-host" in sys.argv[1:] or os.environ.get("HERMES_TALK_REQUIRE_HOST") == "1"
    print("== stage 1: in-memory history, stand-in events")
    ok1, n1 = stage_memory()
    print("== stage 2: real Hermes runtime, adapter events, registered gateway hook, real SessionDB")
    python = hermes_python()
    if python is None:
        print("SKIPPED host stage: no Hermes runtime found; set HERMES_TALK_HERMES_PYTHON")
        ok2 = not require_host
    else:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HERMES_DISABLE_LAZY_INSTALLS="1")
        proc = subprocess.run([python, "-B", str(ROOT / "tests" / "thread" / "host.py")], env=env, cwd=str(ROOT),
                              timeout=600)
        ok2 = proc.returncode == 0
    if ok1 and ok2:
        print(f"TALK_THREAD_OK (stage 1: {n1} checks; host stage: {'ran' if python else 'skipped'})")
        return 0
    print("TALK_THREAD_FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
