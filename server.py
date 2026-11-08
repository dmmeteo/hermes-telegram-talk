#!/usr/bin/env python3
"""Pewds Telegram voice test: static page plus authenticated GPT-Live signaling.

The upstream exchange (URL, headers, no-redirect opener, answer checks) is adapted
from Hermes Conduit's plugin_api.py, MIT licensed; see LICENSES/CONDUIT-LICENSE.
"""
import argparse
import base64
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hermes_talk.avatar import BotAvatar, telegram_photo
from hermes_talk.store import TalkError
from hermes_talk.thread_inbox import launch_thread

logger = logging.getLogger("tg-voice")

GPT_LIVE_URL = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
GPT_LIVE_MODEL = "gpt-live-1-codex"
GPT_LIVE_VOICE = "cove"
UPSTREAM_TIMEOUT_S = 30.0
MAX_ANSWER_BYTES = 64 * 1024
MAX_BODY_BYTES = 32 * 1024
MAX_EVENTS_BODY_BYTES = 192 * 1024
MAX_INIT_DATA_CHARS = 4096
INIT_DATA_MAX_AGE_S = 3600
INIT_DATA_FUTURE_SKEW_S = 60
READ_TIMEOUT_S = 10.0
MINT_LIMIT = 6
MINT_WINDOW_S = 60.0
MAX_ACTIVE_EXCHANGES = 2
TALK_LIMITS = {"bootstrap": 30, "new": 10, "events": 120, "end": 30, "task": 20, "tasks": 240, "task/cancel": 20,
               "task/ack": 60, "context": 30, "context/ack": 30, "pulse/feedback": 30, "voice": 20, "avatar": 12}
CALL = {"initData", "conversation_id", "voice_session_id"}
TALK_ROUTES = {"bootstrap": ({"initData"}, set()),
               "session": ({"initData", "sdp"}, {"conversation_id", "launch"}),
               "events": (CALL | {"events"}, set()),
               "end": (CALL, {"readiness"}),
               "new": ({"initData", "conversation_id"}, set()),
               "task": (CALL | {"delegation_item_id", "request"}, set()),
               "tasks": (CALL, set()),
               "task/cancel": ({"initData", "conversation_id", "job_id"}, set()),
               "task/ack": (CALL | {"job_id"}, set()),
               "context": (CALL, set()),
               "context/ack": (CALL | {"revision"}, set()),
               "pulse/feedback": (CALL | {"delegation_item_id", "request"}, set()),
               "voice": ({"initData"}, {"voice", "launch"}), "avatar": ({"initData"}, set())}
READINESS_STAGES = ("bootstrap", "microphone", "offer", "session", "remote", "handshake", "channel", "peer",
                    "playing", "audio", "usable", "greeting", "greeting_done", "ready")
READINESS_FAILURES = re.compile(r"(?:connect|greeting):[a-z_]{1,24}|none")


def log_readiness(value):
    """Log a call's content-free readiness timings: known stage names with integer milliseconds and the unmet gate."""
    if not isinstance(value, dict):
        return
    stages = value.get("stages") if isinstance(value.get("stages"), dict) else {}
    kept = {k: v for k, v in stages.items() if k in READINESS_STAGES and type(v) is int and 0 <= v <= 86_400_000}
    failure = value.get("failure")
    failure = failure if isinstance(failure, str) and READINESS_FAILURES.fullmatch(failure) else "unknown"
    logger.info("call readiness failure=%s stages=%s", failure, json.dumps(kept, sort_keys=True))


AVATAR_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MAX_CONNECTIONS = 32
CALL_ID = re.compile(r"rtc_[A-Za-z0-9_-]+|[0-9a-fA-F-]{36}")
STATIC_DIR = Path(__file__).resolve().parent / "static"
# A configured Talk runtime serves the Talk UI; without one the original voice-test page stays.
TALK_STATIC_DIR = STATIC_DIR / "talk"
STATIC_FILES = {"": ("index.html", "text/html; charset=utf-8"),
                "index.html": ("index.html", "text/html; charset=utf-8"),
                "app.js": ("app.js", "text/javascript; charset=utf-8"),
                "app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; "
       "connect-src 'self'; media-src 'self' blob:; img-src 'self' blob: data:; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'self' https://web.telegram.org")

PERSONA = (
    "Ти — Pewds Voice Test: короткий публічний тестовий голос, пов'язаний з Pewds. "
    "Це лише перевірка живої голосової розмови в Telegram, а не повний агент Pewds. "
    "Говори українською, доки користувач сам не перейде на іншу мову. "
    "Відповідай коротко, природно і спокійно, з легким дружнім гумором.\n\n"
    "У тебе немає інструментів, файлів, пам'яті, історії чатів, інтернету чи задач Hermes. "
    "Не делегуй завдання. Якщо просять щось зробити, перевірити, знайти чи запам'ятати, "
    "чесно скажи, що в цьому голосовому тесті цього не вмієш, і не вигадуй результат.\n\n"
    "Коли тебе перебивають, замовкни і слухай. Не говори першим: мовчи, доки користувач не заговорить."
)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would resend the bearer header to wherever it points.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def post_sdp(url, headers, body):
    """POST the offer; returns (status, answer text, Location). Bodies are never logged."""
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={**headers, "Content-Type": "application/json"})
    try:
        with _opener.open(request, timeout=UPSTREAM_TIMEOUT_S) as response:
            answer = response.read(MAX_ANSWER_BYTES + 1)
            if len(answer) > MAX_ANSWER_BYTES:
                return 502, "", ""
            return response.status, answer.decode("utf-8", "replace"), response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return status, "", ""
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
        logger.warning("upstream exchange failed: %s", type(exc).__name__)
        raise ApiError(502, "Voice service is unreachable from the server. Try Start again in a minute.")


def _jwt_account(token):
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        account = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
    except Exception:
        return ""
    return account.strip() if isinstance(account, str) else ""


def hermes_codex_credentials():
    """(bearer, account id) from Hermes' native Codex sign-in for the active HERMES_HOME."""
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials
    credentials = resolve_codex_runtime_credentials(refresh_if_expiring=True)
    token = str(credentials.get("api_key") or "").strip() if isinstance(credentials, dict) else ""
    return token, _jwt_account(token)


def session_config():
    return {"model": GPT_LIVE_MODEL, "instructions": PERSONA,
            "audio": {"output": {"voice": GPT_LIVE_VOICE}}, "delegation": {"type": "client"}}


def init_data_fields(init_data):
    """The launch fields of initData that verify_init_data already accepted (signed by Telegram)."""
    return dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))


def verify_init_data(init_data, bot_token, now):
    """Telegram user id from HMAC-signed, fresh initData; None otherwise."""
    if not isinstance(init_data, str) or not init_data or len(init_data) > MAX_INIT_DATA_CHARS:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    fields = dict(pairs)
    if len(fields) != len(pairs):
        return None
    received = fields.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(fields["auth_date"])
        user_id = json.loads(fields["user"])["id"]
    except (KeyError, ValueError, TypeError):
        return None
    if not now - INIT_DATA_MAX_AGE_S <= auth_date <= now + INIT_DATA_FUTURE_SKEW_S:
        return None
    if type(user_id) is not int:
        return None
    return user_id


class MintLimiter:
    def __init__(self, limit, window, clock):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits = {}
        self.lock = threading.Lock()

    def acquire(self, key):
        with self.lock:
            now = self.clock()
            recent = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(recent) >= self.limit:
                self.hits[key] = recent
                return False
            self.hits[key] = recent + [now]
            return True


class VoiceApp:
    def __init__(self, bot_token, allowed_ids, origin, base_path="/tg-voice/",
                 resolve=hermes_codex_credentials, post=post_sdp, clock=time.time, talk=None, avatar_fetch=telegram_photo):
        if not bot_token or not allowed_ids:
            raise ValueError("bot token and allowed user ids are required")
        if not base_path.startswith("/") or not base_path.endswith("/"):
            raise ValueError("base path must start and end with /")
        self.bot_token = bot_token
        self.allowed_ids = frozenset(allowed_ids)
        self.origin = origin.rstrip("/")
        self.base_path = base_path
        self.resolve = resolve
        self.post = post
        self.clock = clock
        self.limiter = MintLimiter(MINT_LIMIT, MINT_WINDOW_S, clock)
        self.active = threading.BoundedSemaphore(MAX_ACTIVE_EXCHANGES)
        self.avatar_fetch = avatar_fetch
        self.talk_limiters = {route: MintLimiter(limit, MINT_WINDOW_S, clock) for route, limit in TALK_LIMITS.items()}
        # Without a Talk runtime the server is the original voice test: one route, fixed persona.
        self.talk = self.avatar = None
        if talk is not None:
            self.attach(talk)

    def attach(self, talk):
        """Serve `talk`: its bot avatar and the launch the gateway's /talk handler offers as the Mini App button."""
        self.talk = talk
        path = talk.config.avatar_path
        self.avatar = BotAvatar(self.bot_token, talk.bot_id, (path.read_bytes(), AVATAR_TYPES[path.suffix.lower()]),
                                self.avatar_fetch)
        if talk.inbox is not None:
            talk.inbox.publish(self.origin + self.base_path, talk.config.profile_id, talk.bot_id, self.allowed_ids)

    @property
    def routes(self):
        return tuple(TALK_ROUTES) if self.talk else ("session",)

    def check_request(self, origin, content_type, length, limit=MAX_BODY_BYTES):
        if origin != self.origin:
            raise ApiError(403, "Open the voice test from the Pewds bot in Telegram.")
        if (content_type or "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "Expected a JSON request.")
        try:
            length = int(length)
        except (TypeError, ValueError):
            raise ApiError(411, "Request length is required.")
        if not 0 < length <= limit:
            raise ApiError(413, "Request is too large.")
        return length

    def _authenticated(self, raw, route):
        """(user id, body) only for a well-formed body with signed, fresh, allowlisted initData."""
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Malformed request.")
        required, optional = TALK_ROUTES[route] if self.talk else ({"initData", "sdp"}, set())
        if not isinstance(body, dict) or not required <= set(body) <= required | optional:
            raise ApiError(400, "Unexpected request fields.")
        user_id = verify_init_data(body["initData"], self.bot_token, int(self.clock()))
        if user_id is None:
            raise ApiError(401, "Telegram sign-in expired or is invalid. Close and reopen the voice test from the Pewds bot.")
        if user_id not in self.allowed_ids:
            raise ApiError(403, "This Telegram account is not allowed to use the Pewds voice test.")
        return user_id, body

    def handle(self, route, raw):
        if route == "session":
            return self.create_session(raw)
        user_id, body = self._authenticated(raw, route)
        if not self.talk_limiters[route].acquire(user_id):
            raise ApiError(429, "Too many requests. Wait a minute, then try again.")
        try:
            if route == "avatar":
                return self.avatar.image()
            if route == "bootstrap":
                return self.talk.bootstrap(user_id, self.avatar_url)
            if route == "new":
                return self.talk.new(user_id, body["conversation_id"], self.avatar_url)
            if route == "events":
                return self.talk.events(user_id, body["conversation_id"], body["voice_session_id"], body["events"])
            if route == "end":
                log_readiness(body.get("readiness"))
                return self.talk.end(user_id, body["conversation_id"], body["voice_session_id"])
            if route == "task":
                return self.talk.task(user_id, body["conversation_id"], body["voice_session_id"],
                                      body["delegation_item_id"], body["request"])
            if route == "tasks":
                return self.talk.tasks(user_id, body["conversation_id"], body["voice_session_id"])
            if route == "task/cancel":
                return self.talk.cancel(user_id, body["conversation_id"], body["job_id"])
            if route == "task/ack":
                return self.talk.ack(user_id, body["conversation_id"], body["voice_session_id"], body["job_id"])
            if route == "context":
                return self.talk.context(user_id, body["conversation_id"], body["voice_session_id"])
            if route == "voice":
                _, status = self._thread(body, user_id, peek=True)
                return {**self.talk.set_voice(user_id, body.get("voice")), "thread": {"status": status}}
            if route == "pulse/feedback":
                return self.talk.pulse_feedback(user_id, body["conversation_id"], body["voice_session_id"],
                                                body["delegation_item_id"], body["request"])
            return self.talk.context_ack(user_id, body["conversation_id"], body["voice_session_id"], body["revision"])
        except TalkError as exc:
            raise ApiError(exc.status, exc.message)

    @property
    def avatar_url(self):
        return self.base_path + "avatar"

    def create_session(self, raw):
        user_id, body = self._authenticated(raw, "session")
        sdp = body["sdp"]
        if not isinstance(sdp, str) or not sdp.startswith("v=0"):
            raise ApiError(400, "Malformed WebRTC offer.")
        if body.get("launch") is not None and not isinstance(body["launch"], str):
            raise ApiError(400, "Malformed launch.")  # before any upstream session exists
        if not self.limiter.acquire(user_id):
            raise ApiError(429, "Too many calls started. Wait a minute, then tap Start.")
        if not self.active.acquire(blocking=False):
            raise ApiError(503, "Another call is still connecting. Try Start again shortly.")
        try:
            if not self.talk:
                return {"sdp": self._exchange(sdp, session_config())}
            try:
                conversation_id, instructions, cards, report = self.talk.prepare(user_id, body.get("conversation_id"))
            except TalkError as exc:
                raise ApiError(exc.status, exc.message)
            answer = self._exchange(sdp, self.talk.session_config(instructions, user_id))
            # The call continues only the Telegram chat (and topic) its /talk launch handle proves; never a guessed one.
            thread, _ = self._thread(body, user_id)
            try:
                voice_session_id = self.talk.started(user_id, conversation_id, cards, report["job_ids"],
                                                     report["job_numbers"], thread)
            except TalkError as exc:  # e.g. api/new rebound the conversation during the exchange
                raise ApiError(exc.status, exc.message)
            return {"sdp": answer, "type": "answer", "voice_session_id": voice_session_id,
                    "conversation_id": conversation_id}
        finally:
            self.active.release()

    def _thread(self, body, user_id, peek=False):
        launch = body.get("launch")
        if launch is not None and not isinstance(launch, str):
            raise ApiError(400, "Malformed launch.")
        return launch_thread(init_data_fields(body["initData"]), user_id, self.talk.config.profile_id,
                             self.talk.bot_id, self.talk.inbox, launch, peek=peek)

    def _exchange(self, sdp, session):
        try:
            token, account = self.resolve()
        except Exception as exc:
            logger.warning("Codex sign-in unavailable: %s", type(exc).__name__)
            token, account = "", ""
        if not token or not account or any(c in token + account for c in "\r\n"):
            raise ApiError(503, "The Pewds host has no working ChatGPT sign-in for voice. No API fallback was used.")
        status, answer, location = self.post(GPT_LIVE_URL, {
            "Authorization": f"Bearer {token}",
            "ChatGPT-Account-Id": account,
            "OpenAI-Alpha": "quicksilver=v2",
        }, {"sdp": sdp, "session": session})
        if not 200 <= status < 300:
            logger.warning("upstream rejected session: HTTP %s", status)
            raise ApiError(502, f"Voice service rejected the call (HTTP {status}). No API fallback was used.")
        call_id = urllib.parse.urlparse(location).path.rstrip("/").rsplit("/", 1)[-1]
        if not (isinstance(answer, str) and answer.startswith("v=0") and CALL_ID.fullmatch(call_id)):
            raise ApiError(502, "Voice service returned an invalid answer. No API fallback was used.")
        return answer


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tg-voice"
        sys_version = ""
        timeout = READ_TIMEOUT_S

        def log_message(self, fmt, *args):
            pass

        def log_request(self, code="-", size="-"):
            logger.info("%s %s %s", self.command, urllib.parse.urlsplit(self.path).path[:120], code)

        def _send(self, status, body, content_type, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, payload):
            self._send(status, json.dumps(payload).encode(), "application/json",
                       {"Cache-Control": "no-store"})

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == app.base_path.rstrip("/"):
                self.send_response(308)
                self.send_header("Location", app.base_path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            name = path[len(app.base_path):] if path.startswith(app.base_path) else None
            if name == "avatar" and app.talk:
                avatar = app.talk.config.avatar_path
                self._send(200, avatar.read_bytes(), AVATAR_TYPES[avatar.suffix.lower()], {
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                    "Cache-Control": "no-cache"})
                return
            if name not in STATIC_FILES:
                self._json(404, {"error": "Not found."})
                return
            filename, content_type = STATIC_FILES[name]
            static_dir = TALK_STATIC_DIR if app.talk else STATIC_DIR
            self._send(200, (static_dir / filename).read_bytes(), content_type, {
                "Content-Security-Policy": CSP, "Cache-Control": "no-cache",
                "Permissions-Policy": "microphone=(self), camera=(), geolocation=()"})

        do_HEAD = do_GET

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            route = path[len(app.base_path + "api/"):] if path.startswith(app.base_path + "api/") else None
            if route not in app.routes:
                self._json(404, {"error": "Not found."})
                return
            try:
                length = app.check_request(self.headers.get("Origin"), self.headers.get("Content-Type"),
                                           self.headers.get("Content-Length"),
                                           MAX_EVENTS_BODY_BYTES if route == "events" else MAX_BODY_BYTES)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    self.close_connection = True
                    return
                result = app.handle(route, raw)
                if route == "avatar":
                    image, kind = result
                    self._send(200, image, kind, {"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; sandbox"})
                else:
                    self._json(200, result)
            except ApiError as exc:
                self.close_connection = True
                self._json(exc.status, {"error": exc.message})
            except TimeoutError:
                self.close_connection = True
            except Exception as exc:
                # The type only: a store or runtime error message can carry conversation text.
                logger.warning("request failed: %s", type(exc).__name__)
                self.close_connection = True
                self._json(500, {"error": "The server could not complete this request. Try again shortly."})

    return Handler


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def allowed_ids_from(text):
    ids = set()
    for part in (text or "").split(","):
        part = part.strip()
        if not re.fullmatch(r"[1-9][0-9]{0,19}", part):
            raise ValueError("TG_VOICE_ALLOWED_USER_IDS must be comma-separated numeric Telegram user ids")
        ids.add(int(part))
    return ids


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pewds Telegram voice test server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--origin", default="https://her.dmmeteo.dev")
    parser.add_argument("--base-path", default="/tg-voice/")
    parser.add_argument("--config", help="Hermes Telegram Talk YAML; without it the server stays the original voice test")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("TG_VOICE_BOT_TOKEN", "").strip()
    if not token:
        parser.error("TG_VOICE_BOT_TOKEN is not set")
    try:
        allowed = allowed_ids_from(os.environ.get("TG_VOICE_ALLOWED_USER_IDS"))
    except ValueError as exc:
        parser.error(str(exc))
    app = VoiceApp(token, allowed, args.origin, args.base_path)
    # Bound before any job or report recovery: a second server that cannot serve must not recover anything.
    server = BoundedServer((args.host, args.port), make_handler(app))
    if args.config:
        import fcntl
        from hermes_talk.config import load_config
        from hermes_talk.history import NativeHistory
        from hermes_talk.talk import TalkRuntime
        home = os.environ.get("HERMES_HOME", "").strip()
        if not home:
            parser.error("HERMES_HOME must name the profile Talk binds to")
        config = load_config(args.config)
        home_path = Path(home).resolve()
        profile = home_path.name if home_path.parent.name == "profiles" else "default"
        if profile != config.profile_id:
            parser.error(f"profile.id in the Talk config must be {profile!r}, the HERMES_HOME profile")
        lock_dir = home_path / "plugin-data" / "hermes-talk"
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        serve_lock = open(lock_dir / "serve.lock", "a")
        try:
            fcntl.flock(serve_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            parser.error("another Talk server already serves this profile")
        from hermes_talk import pulse_model
        app.attach(TalkRuntime(config, token, NativeHistory(home), home=home_path))
        # Plugin discovery (this plugin's ctx.llm) runs in the Pulse worker after start, never before serving.
        app.talk.engine.start(loader=pulse_model.load_host)
    logger.info("serving %s on %s:%s for %s", args.base_path, args.host, args.port, app.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
