#!/usr/bin/env python3
"""Offline browser fixture: the real server with a synthetic credential resolver and a fake GPT-Live.

The fake upstream relays the SDP offer to a provider page (provider.html) running in the same
Chromium, which answers with a local oscillator track. Nothing leaves the container.
"""
import io
import json
import logging
import queue
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server  # noqa: E402
from verify import sign_init_data  # noqa: E402

BOT_TOKEN = "123456:synthetic-browser-bot-token"
USER_ID = 1001
APP_PORT, FIXTURE_PORT = 8801, 8802
SYNTHETIC_TOKEN = "fixture.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiZml4dHVyZS1hY2N0In19.sig"

offers = queue.Queue()
answers = queue.Queue()
log_stream = io.StringIO()
stats = {"resolves": 0, "posts": 0}


def resolve():
    stats["resolves"] += 1
    return SYNTHETIC_TOKEN, server._jwt_account(SYNTHETIC_TOKEN)


def relay_post(url, headers, body):
    stats["posts"] += 1
    assert url == server.GPT_LIVE_URL
    assert headers["Authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
    offers.put(body["sdp"])
    try:
        answer = answers.get(timeout=20)
    except queue.Empty:
        return 504, "", ""
    return 201, answer, f"/v1/realtime/calls/rtc_fixture{stats['posts']}"


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/provider.html":
            self._send(200, (Path(__file__).parent / "provider.html").read_bytes(), "text/html")
        elif path == "/offer":
            try:
                self._send(200, {"sdp": offers.get(timeout=25)})
            except queue.Empty:
                self._send(204, b"")
        elif path == "/initdata":
            self._send(200, {"initData": sign_init_data(USER_ID, int(time.time()), token=BOT_TOKEN)})
        elif path == "/state":
            self._send(200, {**stats, "log": log_stream.getvalue(),
                             "bot_token": BOT_TOKEN, "synthetic_token": SYNTHETIC_TOKEN})
        else:
            self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/answer":
            answers.put(body["sdp"])
            self._send(200, {})
        else:
            self._send(404, {})


def main():
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    server.logger.addHandler(handler)
    server.logger.setLevel(logging.INFO)
    app = server.VoiceApp(BOT_TOKEN, {USER_ID}, f"http://127.0.0.1:{APP_PORT}", "/tg-voice/",
                          resolve=resolve, post=relay_post)
    # The browser run starts ~10 calls in one minute; the per-user mint limit itself is covered by verify.py --offline.
    app.limiter.limit = 50
    app_server = server.BoundedServer(("127.0.0.1", APP_PORT), server.make_handler(app))
    fixture_server = ThreadingHTTPServer(("127.0.0.1", FIXTURE_PORT), Fixture)
    fixture_server.daemon_threads = True
    threading.Thread(target=app_server.serve_forever, daemon=True).start()
    print("FIXTURE READY", flush=True)
    fixture_server.serve_forever()


if __name__ == "__main__":
    main()
