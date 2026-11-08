"""Talk server with real voice tasks for the Chromium delegation job. Synthetic identities, context and media.

Tasks run as real `hermes chat` processes against the labelled scripted model; the provider peer is a
local Chromium page.
"""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402
from hermes_talk import fixtures as f  # noqa: E402
from hermes_talk.config import parse_config  # noqa: E402
from scripted_model import ScriptedModel  # noqa: E402

model = ScriptedModel()
root = Path(tempfile.mkdtemp(prefix="talk-browser-agent-"))
home, work = root / "profiles" / "synthetic", root / "work"
work.mkdir(parents=True)
f.write_home(home)
(home / "config.yaml").write_text(f"model:\n  default: scripted-deterministic-model\n  provider: custom\n"
                                  f"  base_url: {model.base_url}\n  api_key: synthetic-local-key\n")
os.environ["HERMES_HOME"] = str(home)
from hermes_talk.history import NativeHistory  # noqa: E402
from hermes_talk.talk import TalkRuntime  # noqa: E402

marker = "BROWSER-FIXTURE-" + os.urandom(6).hex()
(work / "fixture.txt").write_text(marker + " " + "синтетичний текст " * 40 + "\n")
config = parse_config(f.config_data(tasks={"enabled": True, "user_ids": [f.USER_ID], "toolsets": ["file"],
                                           "workdir": str(work), "run_budget_seconds": 120}), home)
history = NativeHistory(home)
pulse_session = history.create_session("Synthetic browser pulse", "cli")
history.append(pulse_session, [{"role": "assistant", "content": "synthetic pulse ready", "platform_message_id": "bp1",
                                "timestamp": time.time()}])
talk = TalkRuntime(config, f.BOT_TOKEN, history, home=home)
offers, answers, sessions = queue.Queue(), queue.Queue(), []


def relay(url, headers, body):
    assert url == server.GPT_LIVE_URL
    sessions.append(body["session"]["instructions"])
    offers.put(body["sdp"])
    try:
        answer = answers.get(timeout=20)
    except queue.Empty:
        return 504, "", ""
    return 201, answer, f"/v1/realtime/calls/rtc_fixture{len(sessions)}"


app = server.VoiceApp(f.BOT_TOKEN, {f.USER_ID}, "http://127.0.0.1:8811",
                      resolve=lambda: (f.SYNTHETIC_TOKEN, server._jwt_account(f.SYNTHETIC_TOKEN)), post=relay, talk=talk, avatar_fetch=lambda *_: None)
app.limiter.limit = 100
for limiter in app.talk_limiters.values():
    limiter.limit = 1000


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, data, content_type="application/json"):
        raw = data if isinstance(data, bytes) else json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path == "/provider.html":
            return self.send(200, (Path(__file__).parent / "provider.html").read_bytes(), "text/html")
        if self.path == "/offer":
            try:
                return self.send(200, {"sdp": offers.get(timeout=20)})
            except queue.Empty:
                return self.send(204, b"")
        if self.path == "/identity":
            return self.send(200, {"initData": f.sign_init_data(f.USER_ID, int(time.time())), "marker": marker,
                                   "fixture": str(work / "fixture.txt")})
        if self.path == "/state":
            jobs = [{**j.public(), "delivered_via": j.delivered_via, "acks": j.acks,
                     "native_session_id": j.native_session_id} for j in talk.board.jobs.values()]
            cards = [{k: c.get(k) for k in ("title", "state", "exposures", "mentions", "interrupted", "feedback",
                                            "acknowledged")} for c in talk.pulse.cards(talk.account(f.USER_ID))]
            return self.send(200, {"jobs": jobs, "sessions": sessions, "pulse": cards,
                                   "runs": [str(m[-1].get("content"))[-200:] for m in model.requests
                                            if m and m[-1].get("role") == "user"]})
        self.send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/answer":
            answers.put(body["sdp"])
            return self.send(200, {})
        self.send(404, {})


if __name__ == "__main__":
    httpd = server.BoundedServer(("127.0.0.1", 8811), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("TALK AGENT FIXTURE READY", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8812), Fixture).serve_forever()
