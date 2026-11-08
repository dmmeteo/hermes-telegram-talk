"""Offline upgrade fixture. Browser HTTP caches remain enabled; all identity and text are synthetic."""
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import server
from hermes_talk import fixtures as f
from hermes_talk.config import parse_config
from hermes_talk.history import MemoryHistory
from hermes_talk.talk import TalkRuntime
import tempfile

home = f.write_home(tempfile.mkdtemp(prefix="mobile-synthetic-"))
data = f.config_data(greeting={"enabled": False, "text": "Synthetic greeting."})
if os.environ.get("TALK_TEST_AVATAR"):
    data["profile"]["avatar"] = os.environ["TALK_TEST_AVATAR"]
runtime = TalkRuntime(parse_config(data, home), f.BOT_TOKEN, MemoryHistory(),
                      soul=f.file_soul(home), skill=f.file_skill(home))
upstream = f.FakeUpstream()
app = server.VoiceApp(f.BOT_TOKEN, {f.USER_ID}, "http://127.0.0.1:8801",
                      resolve=upstream.resolve, post=upstream.post, talk=runtime, avatar_fetch=lambda *_: None)
app.limiter.limit = 1000
for limiter in app.talk_limiters.values():
    limiter.limit = 1000
mode = "current"
requests = []
Base = server.make_handler(app)


class Handler(Base):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        requests.append(self.path)
        if path == "/fixture/state":
            return self._json(200, {"requests": requests, "mode": mode,
                                    "initData": f.sign_init_data(f.USER_ID, int(time.time()))})
        if path == "/tg-voice/telegram.js":
            return self._send(200, b"window.Telegram={WebApp:{ready(){},expand(){}}};", "text/javascript")
        if path == "/tg-voice/":
            folder = ROOT / "static" if mode == "legacy" else ROOT / "static/talk"
            html = (folder / "index.html").read_text().replace("https://telegram.org/js/telegram-web-app.js", "telegram.js")
            return self._send(200, html.encode(), "text/html", {"Cache-Control": "no-store"})
        if path in ("/tg-voice/app.js", "/tg-voice/app.css"):
            folder = ROOT / "static" if mode == "legacy" else ROOT / "static/talk"
            kind = "text/javascript" if path.endswith(".js") else "text/css"
            return self._send(200, (folder / Path(path).name).read_bytes(), kind,
                              {"Cache-Control": "public, max-age=14400"})
        return super().do_GET()

    def do_POST(self):
        global mode
        requests.append(self.path)
        if self.path == "/fixture/mode":
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            assert payload["mode"] in ("legacy", "current")
            mode = payload["mode"]
            return self._json(200, {"ok": True})
        return super().do_POST()


if __name__ == "__main__":
    print("MOBILE FIXTURE READY", flush=True)
    server.BoundedServer(("127.0.0.1", 8801), Handler).serve_forever()
