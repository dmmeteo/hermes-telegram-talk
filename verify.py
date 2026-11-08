#!/usr/bin/env python3
"""Offline behavioral checks for the Pewds voice test server, plus the browser fixture run.

  python3 verify.py --offline            security, signaling and static checks (no network, no credentials)
  python3 verify.py --browser OUT_DIR    real Chromium against browser/fixture.py in a capped --network none container
"""
import argparse
import hashlib
import hmac
import http.client
import http.server
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

BOT_TOKEN = "123456:synthetic-offline-bot-token"
ALLOWED = 1001
ORIGIN = "https://her.dmmeteo.dev"
SECRET_TOKEN = "header.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoic3ludGgtYWNjdCJ9fQ.sig"
OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=offer-marker\r\n"
ANSWER = "v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\ns=answer\r\n"


def sign_init_data(user_id, auth_date, token=BOT_TOKEN, extra=None):
    fields = {"auth_date": str(auth_date), "query_id": "AAEsynthetic",
              "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":"))}
    fields.update(extra or {})
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


class Fakes:
    def __init__(self):
        self.resolves = 0
        self.posts = []
        self.status = 201
        self.answer = ANSWER
        self.location = "/v1/realtime/calls/rtc_offline123"
        self.resolve_error = None
        self.gate = None

    def resolve(self):
        self.resolves += 1
        if self.resolve_error:
            raise self.resolve_error
        return SECRET_TOKEN, server._jwt_account(SECRET_TOKEN)

    def post(self, url, headers, body):
        self.posts.append((url, headers, body))
        if self.gate:
            self.gate.wait(5)
        return self.status, self.answer, self.location


class Harness:
    def __init__(self):
        self.fakes = Fakes()
        self.now = int(time.time())
        self.app = server.VoiceApp(BOT_TOKEN, {ALLOWED}, ORIGIN, "/tg-voice/",
                                   resolve=self.fakes.resolve, post=self.fakes.post, clock=lambda: self.now)
        self.httpd = server.BoundedServer(("127.0.0.1", 0), server.make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = {"Origin": ORIGIN, "Content-Type": "application/json"} if method == "POST" else {}
        hdrs.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers={k: v for k, v in hdrs.items() if v is not None})
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), payload

    def session(self, body=None, **kw):
        if body is None:
            body = {"initData": sign_init_data(ALLOWED, self.now), "sdp": OFFER}
        status, headers, payload = self.request("POST", "/tg-voice/api/session", body, **kw)
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        return status, data


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def expect_rejected(h, status, data, code):
    assert status == code, f"status {status} != {code}: {data}"
    assert isinstance(data, dict) and data.get("error"), data
    assert h.fakes.resolves == 0, "credentials were resolved"
    assert not h.fakes.posts, "upstream was called"


@check
def valid_session_exchanges_fixed_upstream(h):
    status, data = h.session()
    assert status == 200 and data == {"sdp": ANSWER}, (status, data)
    assert h.fakes.resolves == 1 and len(h.fakes.posts) == 1
    url, headers, body = h.fakes.posts[0]
    assert url == "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
    assert "/v1/realtime/sessions" not in url
    assert headers == {"Authorization": f"Bearer {SECRET_TOKEN}", "ChatGPT-Account-Id": "synth-acct",
                       "OpenAI-Alpha": "quicksilver=v2"}, headers
    assert set(body) == {"sdp", "session"} and body["sdp"] == OFFER
    session = body["session"]
    assert session["model"] == "gpt-live-1-codex" and session["audio"]["output"]["voice"] == "cove"
    assert "Pewds" in session["instructions"] and "українською" in session["instructions"]
    assert "не повний агент" in session["instructions"]
    assert set(session) == {"model", "instructions", "audio", "delegation"}


@check
def tampered_init_data_rejected(h):
    init = sign_init_data(ALLOWED, h.now).replace("Test", "Evil")
    expect_rejected(h, *h.session({"initData": init, "sdp": OFFER}), 401)


@check
def wrong_bot_token_rejected(h):
    init = sign_init_data(ALLOWED, h.now, token="999:other-bot")
    expect_rejected(h, *h.session({"initData": init, "sdp": OFFER}), 401)


@check
def stale_init_data_rejected(h):
    init = sign_init_data(ALLOWED, h.now - server.INIT_DATA_MAX_AGE_S - 1)
    expect_rejected(h, *h.session({"initData": init, "sdp": OFFER}), 401)


@check
def future_init_data_rejected(h):
    init = sign_init_data(ALLOWED, h.now + server.INIT_DATA_FUTURE_SKEW_S + 5)
    expect_rejected(h, *h.session({"initData": init, "sdp": OFFER}), 401)


@check
def wrong_user_rejected(h):
    init = sign_init_data(2002, h.now)
    expect_rejected(h, *h.session({"initData": init, "sdp": OFFER}), 403)


@check
def unsafe_user_and_query_ids_ignored(h):
    unsigned = urllib.parse.urlencode({"user": json.dumps({"id": ALLOWED}), "auth_date": h.now})
    status, _, payload = h.request("POST", f"/tg-voice/api/session?user_id={ALLOWED}",
                                   {"initData": unsigned, "sdp": OFFER})
    expect_rejected(h, status, json.loads(payload), 401)
    expect_rejected(h, *h.session({"initData": "", "sdp": OFFER}), 401)


@check
def duplicate_and_malformed_fields_rejected(h):
    init = sign_init_data(ALLOWED, h.now) + "&user=" + urllib.parse.quote(json.dumps({"id": ALLOWED}))
    expect_rejected(h, *h.session({"initData": init, "sdp": OFFER}), 401)
    expect_rejected(h, *h.session({"initData": 42, "sdp": OFFER}), 401)
    bool_user = sign_init_data(True, h.now)
    expect_rejected(h, *h.session({"initData": bool_user, "sdp": OFFER}), 401)


@check
def missing_init_data_rejected(h):
    expect_rejected(h, *h.session({"sdp": OFFER}), 400)


@check
def unknown_fields_rejected(h):
    base = {"initData": sign_init_data(ALLOWED, h.now), "sdp": OFFER}
    for extra in ({"billing": "api"}, {"url": "https://evil.example/"}, {"model": "gpt-4o-realtime"},
                  {"session": {"model": "x"}}):
        expect_rejected(h, *h.session({**base, **extra}), 400)
    expect_rejected(h, *h.session([base]), 400)


@check
def foreign_or_missing_origin_rejected(h):
    for origin in ("https://evil.example", "null", None, ORIGIN + ".evil.example"):
        expect_rejected(h, *h.session(headers={"Origin": origin}), 403)


@check
def non_json_rejected(h):
    expect_rejected(h, *h.session(headers={"Content-Type": "text/plain"}), 415)
    expect_rejected(h, *h.session(headers={"Content-Type": "application/x-www-form-urlencoded"}), 415)


@check
def oversized_body_rejected_unread(h):
    big = {"initData": sign_init_data(ALLOWED, h.now), "sdp": "v=0" + "a" * server.MAX_BODY_BYTES}
    expect_rejected(h, *h.session(big), 413)


@check
def malformed_body_and_sdp_rejected(h):
    status, _, payload = h.request("POST", "/tg-voice/api/session", raw=b"{not json")
    expect_rejected(h, status, json.loads(payload), 400)
    init = sign_init_data(ALLOWED, h.now)
    status, data = h.session({"initData": init, "sdp": "hello"})
    expect_rejected(h, status, data, 400)


@check
def slow_body_is_cut_off(h):
    sock = socket.create_connection(("127.0.0.1", h.port), timeout=30)
    sock.sendall(b"POST /tg-voice/api/session HTTP/1.1\r\nHost: x\r\nOrigin: " + ORIGIN.encode() +
                 b"\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{\"initData\":")
    started = time.monotonic()
    data = sock.recv(1024)
    elapsed = time.monotonic() - started
    sock.close()
    assert data == b"", data
    assert elapsed < server.READ_TIMEOUT_S + 3, elapsed
    assert h.fakes.resolves == 0 and not h.fakes.posts


@check
def mint_rate_is_limited_per_user(h):
    for _ in range(server.MINT_LIMIT):
        assert h.session()[0] == 200
    status, data = h.session()
    assert status == 429 and len(h.fakes.posts) == server.MINT_LIMIT, (status, data)
    h.now += int(server.MINT_WINDOW_S) + 1
    assert h.session()[0] == 200


@check
def concurrency_is_bounded(h):
    h.fakes.gate = threading.Event()
    results = []
    threads = [threading.Thread(target=lambda: results.append(h.session()[0]))
               for _ in range(server.MAX_ACTIVE_EXCHANGES)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 5
    while len(h.fakes.posts) < server.MAX_ACTIVE_EXCHANGES and time.monotonic() < deadline:
        time.sleep(0.02)
    status, data = h.session()
    h.fakes.gate.set()
    for t in threads:
        t.join(10)
    assert status == 503, (status, data)
    assert sorted(results) == [200] * server.MAX_ACTIVE_EXCHANGES, results
    assert len(h.fakes.posts) == server.MAX_ACTIVE_EXCHANGES


@check
def credential_failure_is_503_without_upstream(h):
    h.fakes.resolve_error = RuntimeError(f"refresh failed for {SECRET_TOKEN}")
    status, data = h.session()
    assert status == 503 and "No API fallback" in data["error"] and not h.fakes.posts, (status, data)
    assert SECRET_TOKEN not in json.dumps(data)


@check
def credential_without_account_is_503(h):
    h.app.resolve = lambda: ("opaque-token-without-claims", "")
    status, data = h.session()
    assert status == 503 and not h.fakes.posts, (status, data)


@check
def upstream_rejection_and_bad_answers_are_502(h):
    for status_code, answer, location in ((401, "", ""), (201, "not-sdp", "/calls/rtc_ok"),
                                          (201, ANSWER, "/calls/../../etc"), (201, ANSWER, "")):
        h.fakes.status, h.fakes.answer, h.fakes.location = status_code, answer, location
        status, data = h.session()
        assert status == 502 and set(data) == {"error"}, (status_code, answer, location, status, data)
        h.now += int(server.MINT_WINDOW_S) + 1


@check
def static_page_is_public_and_minimal(h):
    status, headers, body = h.request("GET", "/tg-voice/")
    assert status == 200 and "frame-ancestors" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    html = body.decode()
    assert re.findall(r"<button[^>]*>([^<]*)</button>", html) == ["Start", "Mute", "End"]
    assert 'id="start"' in html and 'id="mute"' in html and "disabled" in html
    for banned in ("transcript", "waveform", "history", "settings", "status", "<select", "<input", "<canvas"):
        assert banned not in html.lower(), banned
    js = (ROOT / "static/app.js").read_text().lower()
    for banned in ("transcript", "localstorage", "sessionstorage", "console.", "innerhtml", "getstats"):
        assert banned not in js, banned
    assert h.request("GET", "/tg-voice/app.js")[0] == 200
    assert h.request("GET", "/tg-voice")[0] == 308
    for path in ("/tg-voice/../server.py", "/tg-voice/%2e%2e/server.py", "/server.py", "/tg-voice/verify.py",
                 "/tg-voice/static/app.js", "/"):
        assert h.request("GET", path)[0] == 404, path
    assert h.request("GET", "/tg-voice/api/session")[0] == 404
    assert h.request("POST", "/tg-voice/other", {})[0] == 404
    assert h.fakes.resolves == 0 and not h.fakes.posts


@check
def logs_never_carry_secrets_or_sdp(h):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    server.logger.addHandler(handler)
    server.logger.setLevel(logging.INFO)
    try:
        h.session()
        h.fakes.status = 500
        h.session()
        h.fakes.resolve_error = RuntimeError(SECRET_TOKEN)
        h.session()
        h.session({"initData": sign_init_data(2002, h.now), "sdp": OFFER})
    finally:
        server.logger.removeHandler(handler)
    text = stream.getvalue()
    assert text.strip(), "expected request log lines"
    for secret in (SECRET_TOKEN, "synth-acct", "offer-marker", "v=0", "hash=", "query_id", BOT_TOKEN, "2002"):
        assert secret not in text, secret


@check
def real_post_refuses_redirects(h):
    hits = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits.append((self.path, self.headers.get("Authorization")))
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

    upstream = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        status, answer, _ = server.post_sdp(f"http://127.0.0.1:{upstream.server_address[1]}/calls",
                                            {"Authorization": "Bearer x"}, {"sdp": OFFER, "session": {}})
    finally:
        upstream.shutdown()
        upstream.server_close()
    assert status == 307 and answer == "", (status, answer)
    assert [p for p, _ in hits] == ["/calls"], hits


@check
def real_post_bounds_answer_and_unreachable(h):
    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b"v=0" + b"a" * server.MAX_ANSWER_BYTES
            self.send_response(201)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        status, answer, _ = server.post_sdp(f"http://127.0.0.1:{upstream.server_address[1]}/c", {}, {})
    finally:
        upstream.shutdown()
        upstream.server_close()
    assert status == 502 and answer == ""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    try:
        server.post_sdp(f"http://127.0.0.1:{closed_port}/c", {}, {})
    except server.ApiError as exc:
        assert exc.status == 502
    else:
        raise AssertionError("unreachable upstream did not raise")


@check
def config_requires_token_and_numeric_allowlist(h):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TG_VOICE_")}
    run = lambda extra: subprocess.run([sys.executable, str(ROOT / "server.py"), "--port", "0"],
                                       env={**env, **extra}, capture_output=True, text=True, timeout=10)
    out = run({"TG_VOICE_ALLOWED_USER_IDS": "1001"})
    assert out.returncode == 2 and "TG_VOICE_BOT_TOKEN" in out.stderr, out
    out = run({"TG_VOICE_BOT_TOKEN": BOT_TOKEN})
    assert out.returncode == 2 and BOT_TOKEN not in out.stderr + out.stdout, out
    for bad in ("1001,abc", "-5", "1001,,", "1e3", "0"):
        out = run({"TG_VOICE_BOT_TOKEN": BOT_TOKEN, "TG_VOICE_ALLOWED_USER_IDS": bad})
        assert out.returncode == 2, (bad, out)
    assert server.allowed_ids_from(" 1001, 2002 ") == {1001, 2002}
    import inspect
    source = inspect.getsource(server.main)
    assert 'default="127.0.0.1"' in source and 'default="https://her.dmmeteo.dev"' in source
    assert 'default="/tg-voice/"' in source


@check
def credentials_resolved_via_hermes_only(h):
    src = (ROOT / "server.py").read_text()
    assert "from hermes_cli.auth_codex import resolve_codex_runtime_credentials" in src
    assert "/v1/realtime/sessions" not in src and "api.openai.com/v1" not in src
    js = (ROOT / "static/app.js").read_text()
    assert "initDataUnsafe" not in js and "chatgpt.com" not in js and "Bearer" not in js
    assert server._jwt_account("x.eyJmb28iOjF9.y") == ""


def run_offline():
    failures = 0
    for fn in CHECKS:
        h = Harness()
        try:
            fn(h)
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        finally:
            h.close()
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} offline checks passed")
    return 1 if failures else 0


def run_browser(out_dir):
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "--rm", "--init", "--network", "none",
           "--memory", "1g", "--memory-swap", "1g", "--cpus", "1", "--pids-limit", "384", "--shm-size", "256m",
           "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
           "--tmpfs", "/tmp:rw,size=256m", "--env", "HOME=/tmp/home", "--env", "TMPDIR=/tmp",
           "--env", "PYTHONDONTWRITEBYTECODE=1",
           "--mount", f"type=bind,src={ROOT},dst=/src,readonly",
           "--mount", f"type=bind,src={ROOT / 'browser/run.mjs'},dst=/app/job.mjs,readonly",
           "--mount", f"type=bind,src={out},dst=/output",
           "--entrypoint", "node", "django-admin-repl-verify-browser:pw1.63.0", "/app/job.mjs"]
    return subprocess.run(cmd, timeout=300).returncode


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--browser", metavar="OUT_DIR")
    args = parser.parse_args()
    return run_offline() if args.offline else run_browser(args.browser)


if __name__ == "__main__":
    sys.exit(main())
