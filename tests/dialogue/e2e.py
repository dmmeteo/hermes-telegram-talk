#!/usr/bin/env python3
"""The real app.js against the real Talk server (in-memory history, fake upstream, synthetic identity, no network).

Per provider behaviour it checks the opening contract across the boundary: the server's speak-first opening in the
configured language (Ukrainian here), the client's opening requests (none when the model greets by itself, exactly
one when it stays silent, none when the greeting is off), and that both transcript roles of the call are stored.
Usage: python3 tests/dialogue/e2e.py   (needs node on PATH)
"""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402
from hermes_talk import fixtures as fx  # noqa: E402
from hermes_talk.config import parse_config  # noqa: E402
from hermes_talk.history import MemoryHistory  # noqa: E402
from hermes_talk.talk import TalkRuntime  # noqa: E402

UK_GREETING = "Привіт! Я на зв’язку. Слухаю."
SCENARIOS = {
    "model greets by itself: no client opening request": ({}, "self-greet", 0, 1),
    "model stays silent: exactly one opening request, one greeting": ({}, "silent", 1, 1),
    "greeting off: no opening request, model waits for the user": ({"greeting": {"enabled": False}}, "none", 0, 0),
}


def scenario(name, overrides, provider, requests, greetings):
    home = fx.write_home(tempfile.mkdtemp(prefix="talk-dialogue-"))
    try:
        fake = fx.FakeUpstream()
        config = parse_config(fx.config_data(voice={"language": "uk"}, **overrides), home)
        history = MemoryHistory()
        talk = TalkRuntime(config, fx.BOT_TOKEN, history, skill=fx.file_skill(home))
        app = server.VoiceApp(fx.BOT_TOKEN, {fx.USER_ID}, fx.ORIGIN, "/tg-voice/", resolve=fake.resolve,
                              post=fake.post, talk=talk)
        httpd = server.BoundedServer(("127.0.0.1", 0), server.make_handler(app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}/tg-voice/"
        import time
        out = subprocess.run(["node", "tests/dialogue/e2e.mjs", "static/talk/app.js", base, fx.ORIGIN,
                              fx.sign_init_data(fx.USER_ID, int(time.time())), provider],
                             cwd=ROOT, capture_output=True, text=True, timeout=90)
        httpd.shutdown(); httpd.server_close()
        line = next((l for l in out.stdout.splitlines() if l.startswith("DIALOGUE_E2E ")), None)
        assert out.returncode == 0 and line, f"node driver failed: {out.stdout[-2000:]} {out.stderr[-2000:]}"
        result = json.loads(line.split(" ", 1)[1])
        assert result["error"] is None, result
        instructions = fake.posts[-1][2]["session"]["instructions"]
        opening = instructions.rsplit("\n\n", 1)[1]
        if overrides.get("greeting", {}).get("enabled", True):
            assert "Opening policy: speak first." in opening and "Ukrainian" in opening and UK_GREETING in opening, opening
        else:
            assert "Opening policy: do not speak first." in opening, opening
        assert result["openingRequests"] == requests, (name, result)
        assert result["greetingsSpoken"] == greetings, (name, result)
        cid = talk.bootstrap(fx.USER_ID, "/tg-voice/avatar")["conversation_id"]
        stored = [(r, t) for r, t in history.recent(cid, 50)]
        assert ("user", "Синтетичне питання.") in stored and ("assistant", "Синтетична відповідь.") in stored, stored
        if greetings:
            assert stored.count(("assistant", UK_GREETING)) == 1, f"one greeting stored once: {stored}"
        assert len(stored) == len(set(stored)), f"no turn stored twice: {stored}"
        return result
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main():
    failed = 0
    for name, args in SCENARIOS.items():
        try:
            result = scenario(name, *args)
            print("PASS", name, json.dumps({k: result[k] for k in ("openingRequests", "greetingsSpoken", "readiness")},
                                           ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL", name, type(exc).__name__, str(exc)[:3000])
    print(f"{len(SCENARIOS) - failed}/{len(SCENARIOS)} dialogue e2e checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
