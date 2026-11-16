#!/usr/bin/env python3
"""The Talk process never writes its Telegram bot token to a log or a stored status error.

  python3 tests/logsecrets/run.py [--require-host]    prints TALK_LOG_SECRETS_OK

Runs server.main() up to the point where it would serve (so the real logging setup is the one under test), then
sends and edits through the real Hermes Telegram sender (python-telegram-bot, httpx) against a local fake Bot API
that answers with success, 401, 500 and a refused connection, and through a real httpx request that fails with
httpx.HTTPStatusError (whose message carries the full request URL). The bot token is synthetic. Every byte written
to the process' stderr (what journald keeps) and every error Talk stores is searched for it.
Needs a Hermes runtime with python-telegram-bot; without one it is SKIPPED, and --require-host makes that a failure.
"""
import glob
import http.server
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOT, USER = "123456789", 1001
TOKEN = f"{BOT}:TEST-notreal-0123456789abcdefGHIJKLMNOP"  # a real token's shape and length: redact() keys on both
MESSAGE = {"message_id": 8001, "date": 0, "chat": {"id": USER, "type": "private"}, "text": "hello"}


PROBE = "import telegram, tools.send_message_senders as s; s._telegram_bot"


def hermes_python():
    """HERMES_TALK_HERMES_PYTHON, else the newest-listed runtime that has python-telegram-bot and the sender."""
    explicit = os.environ.get("HERMES_TALK_HERMES_PYTHON")
    if explicit:
        return explicit
    for candidate in sorted(glob.glob(os.path.expanduser("~/.hermes/installs/*/environments/*/venv/bin/python")),
                            reverse=True):
        if subprocess.run([candidate, "-c", PROBE], capture_output=True, timeout=120).returncode == 0:
            return candidate
    return None


class FakeBotApi:
    """A local stand-in for api.telegram.org: answers per `mode`, records the paths it was asked."""

    def __init__(self):
        outer = self
        self.mode, self.paths = "ok", []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.paths.append(self.path)
                status, body = {"ok": (200, {"ok": True, "result": MESSAGE}),
                                "unauthorized": (401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
                                "server_error": (500, {"ok": False, "error_code": 500, "description": "boom"})
                                }[outer.mode]
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/bot"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


MARK = [0]  # where the current check's own log output starts


class Stop(Exception):
    pass


def start_talk_logging(stderr):
    """What `server.py` does at start-up, by running its own main() until it would bind a port."""
    import server
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.WARNING)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    real_stderr, real_bound = sys.stderr, server.BoundedServer

    def stop(*args, **kwargs):
        raise Stop

    os.environ.update(TG_VOICE_BOT_TOKEN=TOKEN, TG_VOICE_ALLOWED_USER_IDS=str(USER))
    sys.stderr, server.BoundedServer = stderr, stop
    try:
        server.main(["--port", "0"])
    except Stop:
        pass
    finally:
        sys.stderr, server.BoundedServer = real_stderr, real_bound


def check_no_token(label, texts, stderr, api, arrived=True):
    log = stderr.getvalue()[MARK[0]:]
    assert api.paths or not arrived, f"{label}: the request never reached the fake Bot API, so this proves nothing"
    leaked = [name for name, text in (*texts, ("log", log)) if TOKEN in text or TOKEN.split(":")[1] in text]
    assert not leaked, f"{label}: the bot token reached {leaked}"


def main():
    try:
        exec(PROBE)
    except Exception:
        runtime = hermes_python()
        if runtime and Path(runtime).resolve() != Path(sys.executable).resolve():
            os.execv(runtime, [runtime, *sys.argv])
        print("SKIPPED: no Hermes runtime with python-telegram-bot")
        return 1 if "--require-host" in sys.argv[1:] else 0
    sys.path.insert(0, str(ROOT))
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="talk-logsecrets-")
    os.environ.pop("TELEGRAM_PROXY", None)
    import httpx
    import telegram as ptb
    import tools.send_message_senders as senders
    from hermes_talk import receipts
    from hermes_talk.history import MemoryHistory
    from hermes_talk.talk import redact
    spec = importlib.util.spec_from_file_location("reports_run", ROOT / "tests" / "reports" / "run.py")
    reports = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reports)

    api = FakeBotApi()
    stderr = io.StringIO()
    real_bot, real_factory = ptb.Bot, senders._telegram_bot

    class LocalBot(real_bot):
        def __init__(self, token, *args, **kwargs):
            super().__init__(token, *args, base_url=api.base, **kwargs)

    class RaisingBot(LocalBot):
        """A bot whose edit fails the way a raw httpx caller's does: raise_for_status() on the real response."""

        async def edit_message_text(self, *args, **kwargs):
            async with httpx.AsyncClient() as client:
                (await client.post(f"{api.base}{self.token}/editMessageText", json={})).raise_for_status()

    destination = receipts.Destination.private(USER, BOT)
    sender = receipts.TelegramSender(TOKEN, timeout=10.0)
    checks = {}

    def check(fn):
        checks[fn.__name__] = fn
        return fn

    @check
    def send_and_edit_succeed():
        api.mode = "ok"
        sent, edited = sender.send(destination, "hello"), sender.edit(destination, "8001", "hello again")
        assert sent.get("success") and edited.get("success"), (sent, edited)
        check_no_token("success", [("send", json.dumps(sent)), ("edit", json.dumps(edited))], stderr, api)

    @check
    def telegram_refusals_carry_no_token():
        results = []
        for mode in ("unauthorized", "server_error"):
            api.mode = mode
            results += [sender.send(destination, "hello"), sender.edit(destination, "8001", "again")]
        assert all("error" in r for r in results), results
        check_no_token("refusal", [("results", json.dumps(results))], stderr, api)

    @check
    def refused_connection_carries_no_token():
        api.mode = "ok"
        port, api.base = api.port, "http://127.0.0.1:9/bot"  # nothing listens on the discard port
        try:
            results = [sender.send(destination, "hello"), sender.edit(destination, "8001", "again")]
        finally:
            api.base = f"http://127.0.0.1:{port}/bot"
        assert all("error" in r for r in results), results
        check_no_token("connection refused", [("results", json.dumps(results))], stderr, api, arrived=False)

    @check
    def an_exception_that_embeds_the_request_url_is_not_stored():
        """httpx.HTTPStatusError's message is the full URL; the sender's edit error is stored on the report."""
        history, tracker = MemoryHistory(), []

        class Spy(receipts.TelegramSender):
            def edit(self, *args):
                tracker.append(super().edit(*args))
                return tracker[-1]

        desk = reports.desk_for(history, Spy(TOKEN, timeout=10.0), redact=redact)
        j = reports.walk(desk, reports.job(report_to=asdict(destination)), reports.jobs.ACCEPTED)  # the status message is sent normally
        api.mode, ptb.Bot = "server_error", RaisingBot
        try:
            reports.walk(desk, j, reports.jobs.RUNNING, settle_each=False)  # its first edit then fails
            deadline = time.monotonic() + 10
            while not tracker and time.monotonic() < deadline:
                time.sleep(0.05)
            desk.wait_settled(j.id, 2)
        finally:
            ptb.Bot = LocalBot
        assert tracker and TOKEN in tracker[0].get("error", ""), "the edit error must embed the request URL"
        stored = json.dumps(history.meta, sort_keys=True)
        check_no_token("embedded url", [("stored report", stored)], stderr, api)

    @check
    def redact_masks_a_token_inside_a_bot_api_url():
        url = f"Server error '500' for url 'https://api.telegram.org/bot{TOKEN}/editMessageText'"
        check_no_token("redact()", [("redact()", redact(url))], stderr, api, arrived=False)

    @check
    def any_logger_message_or_traceback_is_scrubbed():
        """Hermes' sender, python-telegram-bot or Talk may log an exception whose message is the request URL."""
        api.mode = "server_error"
        try:
            httpx.post(f"{api.base}{TOKEN}/getMe").raise_for_status()
        except httpx.HTTPStatusError as exc:
            for name in ("tools.send_message_senders", "telegram.ext", "tg-voice", "some.other.library"):
                logging.getLogger(name).warning("send failed (%s)", exc)
                logging.getLogger(name).error("with traceback", exc_info=True)
        check_no_token("other loggers", [], stderr, api)
        assert "[bot token]" in stderr.getvalue()[MARK[0]:], "the scrubbed lines are still logged"

    ptb.Bot = LocalBot
    failures = 0
    try:
        start_talk_logging(stderr)
        for name, fn in checks.items():
            MARK[0] = len(stderr.getvalue())
            api.paths.clear()
            try:
                fn()
                print(f"PASS [logsecrets] {name}", flush=True)
            except Exception as exc:
                failures += 1
                print(f"FAIL [logsecrets] {name}: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}",
                      flush=True)
    finally:
        ptb.Bot, senders._telegram_bot = real_bot, real_factory
        api.close()
    print(f"{len(checks) - failures}/{len(checks)} log-secret checks passed; "
          f"{len(stderr.getvalue().splitlines())} log lines searched")
    print("TALK_LOG_SECRETS_" + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
