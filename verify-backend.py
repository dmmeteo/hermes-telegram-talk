#!/usr/bin/env python3
"""Hermes Talk backend checks. Synthetic identities, text and credentials only; no network.

  python3 verify-backend.py --offline

runs, in order:
  1. verify.py --offline, the legacy signaling/security suite (26 checks);
  2. the Talk checks on synthetic in-memory history;
  3. the same Talk checks plus plugin discovery/registration in a disposable HERMES_HOME under a
     real Hermes runtime, which supplies SessionDB, load_soul_md and skill_view. The runtime is
     HERMES_TALK_HERMES_PYTHON, else the first ~/.hermes/installs/*/environments/*/venv/bin/python.
     If none exists the step is reported as SKIPPED with that reason, never as passed.
"""
import argparse
import glob
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import http.client
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import server  # noqa: E402
from hermes_talk import fixtures as fx  # noqa: E402
from hermes_talk.config import ConfigError, parse_config  # noqa: E402
from hermes_talk.history import MemoryHistory  # noqa: E402
from hermes_talk.talk import TalkRuntime  # noqa: E402

PLUGIN_FILES = ("plugin.yaml", "__init__.py", "server.py", "hermes_talk", "static", "LICENSES")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, native, data=None):
        self.native = native
        self.home = fx.write_home(tempfile.mkdtemp(prefix="talk-check-"))
        self.clock = Clock(1_900_000_000.0 if not native else __import__("time").time())
        self.fake = fx.FakeUpstream()
        self.config = parse_config(data or fx.config_data(), self.home)
        if native:
            os.environ["HERMES_HOME"] = str(self.home)
            from hermes_talk.history import NativeHistory
            self.history = NativeHistory(self.home)
            self.talk = TalkRuntime(self.config, fx.BOT_TOKEN, self.history, clock=self.clock)
        else:
            self.history = MemoryHistory()
            self.talk = TalkRuntime(self.config, fx.BOT_TOKEN, self.history, soul=fx.file_soul(self.home),
                                    skill=fx.file_skill(self.home), clock=self.clock)
        self.app = server.VoiceApp(fx.BOT_TOKEN, {fx.USER_ID, fx.OTHER_USER_ID}, fx.ORIGIN, "/tg-voice/",
                                   resolve=self.fake.resolve, post=self.fake.post, clock=self.clock,
                                   talk=self.talk)
        self.httpd = server.BoundedServer(("127.0.0.1", 0), server.make_handler(self.app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.native:
            self.history.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def init(self, user=fx.USER_ID, token=fx.BOT_TOKEN):
        return fx.sign_init_data(user, int(self.clock()), token)

    def post(self, route, body, origin=fx.ORIGIN):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=15)
        headers = {"Content-Type": "application/json", **({"Origin": origin} if origin else {})}
        conn.request("POST", f"/tg-voice/api/{route}", body=json.dumps(body).encode(), headers=headers)
        resp = conn.getresponse()
        status, payload = resp.status, resp.read()
        conn.close()
        return status, json.loads(payload) if payload else None

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=15)
        conn.request("GET", path)
        resp = conn.getresponse()
        out = resp.status, dict(resp.getheaders()), resp.read()
        conn.close()
        return out

    def ok(self, route, body, user=fx.USER_ID):
        status, data = self.post(route, {"initData": self.init(user), **body})
        assert status == 200, (route, status, data)
        return data

    def call(self, conversation_id=None, user=fx.USER_ID):
        body = {"sdp": fx.OFFER, **({"conversation_id": conversation_id} if conversation_id else {})}
        data = self.ok("session", body, user)
        return data, self.fake.posts[-1][2]["session"]["instructions"]

    def seed(self, title, source="cli", user_id=None, turns=(), end_reason=None, failed=False, age_s=0):
        """A synthetic non-Talk session in the same store, as other Hermes work would leave it."""
        at = self.clock() - age_s
        if self.native:
            from hermes_state_ids import new_session_id
            db = self.history.db
            sid = new_session_id()
            db.create_session(sid, source, user_id=user_id)
            db.set_session_title(sid, title)
            for i, (role, text) in enumerate(turns):
                kind = "failed_turn" if failed and i == len(turns) - 1 else None
                db.append_message(sid, role, content=text, timestamp=at, display_kind=kind)
            if end_reason:
                db.end_session(sid, end_reason)
            return sid
        sid = self.history.create_session(title, source, user_id=user_id)
        msgs = [{"role": r, "content": t, "platform_message_id": None, "timestamp": at} for r, t in turns]
        if failed:
            msgs[-1]["display_kind"] = "failed_turn"
        self.history.append(sid, msgs)
        self.history.sessions[sid]["last_active"] = at
        if end_reason:
            self.history.end_session(sid, end_reason, at)
        return sid

    def stored(self, conversation_id):
        return self.history.recent(conversation_id, 100)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def ev(i, seq, role, text):
    return {"id": i, "sequence": seq, "role": role, "text": text}


@check
def bootstrap_returns_frozen_public_shape(h):
    data = h.ok("bootstrap", {})
    assert set(data) == {"conversation_id", "profile", "appearance", "greeting_enabled", "limits"}, data
    assert data["profile"] == {"id": "synthetic", "display_name": "Synthetic Talk", "avatar_url": "/tg-voice/avatar"}
    assert data["appearance"] == {"listen_color": "#4FC3F7", "speak_color": "#FFB74D"}
    assert data["greeting_enabled"] is True and data["limits"] == {"event_batch_max": 20}
    text = json.dumps(data)
    for private in (fx.SOUL_MARKER, fx.SKILL_MARKER, fx.SYNTHETIC_TOKEN, fx.BOT_TOKEN, "instructions"):
        assert private not in text, private
    assert h.ok("bootstrap", {})["conversation_id"] == data["conversation_id"], "bootstrap is stable"
    status, headers, body = h.get("/tg-voice/avatar")
    assert status == 200 and headers["Content-Type"] == "image/svg+xml" and body.startswith(b"<svg")
    assert "sandbox" in headers["Content-Security-Policy"]
    assert h.fake.resolves == 0 and not h.fake.posts


@check
def every_route_rejects_bad_identity_before_any_state(h):
    good = h.ok("bootstrap", {})
    cid = good["conversation_id"]
    meta_before = None if h.native else dict(h.history.meta)
    bodies = {"bootstrap": {}, "session": {"sdp": fx.OFFER, "conversation_id": cid},
              "events": {"conversation_id": cid, "voice_session_id": "vs_" + "a" * 24,
                         "events": [ev("e1", 0, "user", "hi")]},
              "end": {"conversation_id": cid, "voice_session_id": "vs_" + "a" * 24},
              "new": {"conversation_id": cid}}
    for route, body in bodies.items():
        tampered = h.init()[:-4] + "0000"
        assert h.post(route, {"initData": tampered, **body})[0] == 401, route
        assert h.post(route, {"initData": h.init(token="654321:other-bot-token-synthetic"), **body})[0] == 401, route
        assert h.post(route, {"initData": h.init(user=3003), **body})[0] == 403, route
        for origin in ("https://evil.example", None):
            assert h.post(route, {"initData": h.init(), **body}, origin=origin)[0] == 403, route
        for extra in ({"profile": "other"}, {"bot": "1"}, {"user_id": 3003}, {"instructions": "x"}, {"model": "y"}):
            status, data = h.post(route, {"initData": h.init(), **body, **extra})
            assert status == 400 and data["error"], (route, extra, status)
    assert h.fake.resolves == 0 and not h.fake.posts, "no credential or upstream access on rejected requests"
    if meta_before is not None:
        assert h.history.meta == meta_before, "rejected requests wrote state"


@check
def session_composes_soul_allowlisted_skills_and_greeting(h):
    data, instructions = h.call()
    assert set(data) == {"sdp", "type", "voice_session_id", "conversation_id"} and data["type"] == "answer"
    assert data["sdp"] == fx.ANSWER and data["voice_session_id"].startswith("vs_")
    assert data["conversation_id"] == h.ok("bootstrap", {})["conversation_id"]
    assert fx.SOUL_MARKER in instructions, "SOUL via host loader"
    assert fx.SKILL_MARKER in instructions, "allowlisted skill included"
    assert fx.OTHER_SKILL_MARKER not in instructions, "non-allowlisted skill excluded"
    assert fx.SECRET_IN_SOUL not in instructions and "[redacted]" in instructions
    assert "Say hello in one short sentence." in instructions and "nothing was executed" in instructions
    url, headers, body = h.fake.posts[-1]
    assert url == server.GPT_LIVE_URL and set(body) == {"sdp", "session"}
    assert body["session"]["model"] == "gpt-live-1-codex" and body["session"]["audio"]["output"]["voice"] == "cove"
    assert len(instructions) <= h.config.budgets.instructions_chars


@check
def events_are_idempotent_and_order_tolerant(h):
    data, _ = h.call()
    cid, vs = data["conversation_id"], data["voice_session_id"]
    batch = [ev("u2", 2, "user", "second question"), ev("a1", 1, "assistant", "first answer"),
             ev("u0", 0, "user", "first question")]
    body = {"conversation_id": cid, "voice_session_id": vs, "events": batch}
    assert h.ok("events", body) == {"accepted_ids": ["u2", "a1", "u0"], "conversation_id": cid}
    assert h.ok("events", body)["accepted_ids"] == ["u2", "a1", "u0"], "retried batch accepted"
    late = {**body, "events": [ev("a3", 3, "assistant", "second answer"), ev("u0", 0, "user", "first question")]}
    assert h.ok("events", late)["accepted_ids"] == ["a3", "u0"]
    assert h.stored(cid) == [("user", "first question"), ("assistant", "first answer"),
                             ("user", "second question"), ("assistant", "second answer")], h.stored(cid)
    for bad in ([ev("t1", 4, "tool", "x")], [ev("s1", 4, "system", "x")], [ev("x1", -1, "user", "x")],
                [ev("x2", 4, "user", "")], [ev("x3", 4, "user", "y" * 2001)], [ev("x4", 4, "user", "a")] * 2,
                [ev(f"b{i}", i, "user", "z") for i in range(21)], [], [{"id": "r1", "sequence": 1, "role": "user",
                                                                       "text": "x", "audio": "AAAA"}]):
        assert h.post("events", {"initData": h.init(), **body, "events": bad})[0] == 400, bad
    assert len(h.stored(cid)) == 4, "rejected batches wrote nothing"


@check
def foreign_conversations_and_voice_sessions_are_refused(h):
    mine, _ = h.call()
    theirs, _ = h.call(user=fx.OTHER_USER_ID)
    assert mine["conversation_id"] != theirs["conversation_id"], "accounts get separate conversations"
    steal = {"conversation_id": mine["conversation_id"], "voice_session_id": mine["voice_session_id"],
             "events": [ev("x1", 0, "user", "injected")]}
    assert h.post("events", {"initData": h.init(fx.OTHER_USER_ID), **steal})[0] == 404
    assert h.post("end", {"initData": h.init(fx.OTHER_USER_ID), "conversation_id": mine["conversation_id"],
                          "voice_session_id": mine["voice_session_id"]})[0] == 404
    assert h.post("session", {"initData": h.init(fx.OTHER_USER_ID), "sdp": fx.OFFER,
                              "conversation_id": mine["conversation_id"]})[0] == 404
    cross = {"conversation_id": mine["conversation_id"], "voice_session_id": theirs["voice_session_id"],
             "events": [ev("x2", 0, "user", "mixed")]}
    assert h.post("events", {"initData": h.init(), **cross})[0] == 404
    assert h.stored(mine["conversation_id"]) == [] and h.stored(theirs["conversation_id"]) == []


@check
def reopen_resumes_the_same_conversation_with_summary(h):
    data, _ = h.call()
    cid, vs = data["conversation_id"], data["voice_session_id"]
    h.ok("events", {"conversation_id": cid, "voice_session_id": vs,
                    "events": [ev("u1", 0, "user", "remember the blue kettle plan"),
                               ev("a1", 1, "assistant", "noted the blue kettle plan")]})
    assert h.ok("end", {"conversation_id": cid, "voice_session_id": vs}) == {"ok": True}
    assert h.ok("end", {"conversation_id": cid, "voice_session_id": vs}) == {"ok": True}, "end is idempotent"
    h.clock.now += 3 * 3600
    again = h.ok("bootstrap", {})
    assert again["conversation_id"] == cid, "no time-based reset"
    second, instructions = h.call(cid)
    assert second["conversation_id"] == cid and second["voice_session_id"] != vs
    assert "## Earlier in this conversation\n- remember the blue kettle plan" in instructions, instructions
    assert "assistant: noted the blue kettle plan" in instructions
    late = {"conversation_id": cid, "voice_session_id": vs, "events": [ev("u2", 2, "user", "pending after end")]}
    assert h.ok("events", late)["accepted_ids"] == ["u2"], "pending batch of an ended call is kept"


@check
def explicit_new_is_isolated(h):
    first, _ = h.call()
    old = first["conversation_id"]
    h.ok("events", {"conversation_id": old, "voice_session_id": first["voice_session_id"],
                    "events": [ev("u1", 0, "user", "old private topic")]})
    fresh = h.ok("new", {"conversation_id": old})
    assert set(fresh) == {"conversation_id", "profile", "appearance", "greeting_enabled", "limits"}
    assert fresh["conversation_id"] != old and h.ok("bootstrap", {})["conversation_id"] == fresh["conversation_id"]
    _, instructions = h.call(fresh["conversation_id"])
    assert "old private topic" not in instructions
    assert h.stored(old) == [("user", "old private topic")], "the previous conversation stays intact"
    status, data = h.post("session", {"initData": h.init(), "sdp": fx.OFFER, "conversation_id": old})
    assert status == 409 and data["error"]
    assert h.post("new", {"initData": h.init(), "conversation_id": old})[0] == 409


@check
def context_budgets_clip_every_section(h):
    tight = fx.config_data(context={"skills": ["talk-brief", "not-allowed"], "budgets": {
        "instructions_chars": 900, "soul_chars": 60, "skill_chars": 50, "skills_chars": 70,
        "recent_turns": 2, "turn_chars": 20}})
    h2 = Harness(h.native, tight)
    try:
        data, _ = h2.call()
        h2.ok("events", {"conversation_id": data["conversation_id"], "voice_session_id": data["voice_session_id"],
                         "events": [ev(f"u{i}", i, "user", f"turn {i} " + "w" * 40) for i in range(5)]})
        _, instructions = h2.call()
        assert len(instructions) <= 900, len(instructions)
        assert "[clipped]" in instructions
        assert fx.OTHER_SKILL_MARKER not in instructions, "second skill exceeds skills_chars"
        assert "turn 4" in instructions and "turn 3" in instructions and "turn 2" not in instructions
        assert fx.SECRET_IN_SOUL not in instructions
    finally:
        h2.close()
    for bad in ({"profile": {"id": "x", "avatar": "https://cdn.example/a.png?token=1"}},
                {"profile": {"id": "x", "avatar": "missing.svg"}},
                {"profile": {"id": "x"}, "privacy": {"persist_audio": True}},
                {"profile": {"id": "x"}, "context": {"budgets": {"provider_tokens": 128000}}},
                {"profile": {"id": "x"}, "appearance": {"listen_color": "red"}}, {"profile": {}}):
        try:
            parse_config(bad, h.home)
        except ConfigError:
            continue
        raise AssertionError(f"accepted bad config {bad}")


@check
def activity_briefing_is_scoped_truthful_and_cursored(h):
    h.seed("Synthetic deploy check", turns=[("user", "go"), ("assistant", "deployed fine")], end_reason="done",
           age_s=600)
    h.seed("Synthetic broken job", turns=[("user", "go"), ("assistant", "it failed")], failed=True, age_s=500)
    h.seed("Synthetic stale job", turns=[("user", "go")], age_s=7200)
    h.seed("Synthetic other user chat", source="telegram", user_id=str(fx.OTHER_USER_ID),
           turns=[("user", "secret of someone else")], age_s=300)
    h.seed("Synthetic own telegram chat", source="telegram", user_id=str(fx.USER_ID),
           turns=[("user", "hey"), ("assistant", "hello")], age_s=3600)
    h.seed("Synthetic excluded source", source="tool", turns=[("user", "x")], age_s=100)
    h.fake.status = 500
    assert h.post("session", {"initData": h.init(), "sdp": fx.OFFER})[0] == 502
    h.fake.status = 201
    data, instructions = h.call()
    brief = instructions.split("## Activity since the last visit\n", 1)[1].split("\n\n## ", 1)[0]
    assert "[replied] Synthetic deploy check" in brief and "[failed] Synthetic broken job" in brief
    assert "[unknown] Synthetic stale job" in brief and "[replied] Synthetic own telegram chat" in brief
    assert "other user" not in brief and "excluded source" not in brief and "Telegram Voice" not in brief
    assert "[completed]" not in brief and "[active]" not in brief
    assert "cli session " in brief and " UTC)" in brief, "source ids and timestamps preserved"
    cid, vs = data["conversation_id"], data["voice_session_id"]
    _, unchanged = h.call(cid)
    assert "Synthetic broken job" in unchanged, "unread until the assistant actually spoke"
    h.ok("events", {"conversation_id": cid, "voice_session_id": vs,
                    "events": [ev("a1", 0, "assistant", "two updates since last time")]})
    h.clock.now += 60
    h.seed("Synthetic new job", turns=[("user", "go")], age_s=0)
    _, later = h.call(cid)
    later_brief = later.split("## Activity since the last visit\n", 1)[1]
    assert "[active] Synthetic new job" in later_brief and "Synthetic broken job" not in later_brief


@check
def provider_and_credential_failures_create_no_voice_session(h):
    cid = h.ok("bootstrap", {})["conversation_id"]
    h.fake.status = 500
    status, data = h.post("session", {"initData": h.init(), "sdp": fx.OFFER, "conversation_id": cid})
    assert status == 502 and "No API fallback" in data["error"]
    h.fake.status = 201
    h.fake.resolve_error = RuntimeError(fx.SYNTHETIC_TOKEN)
    status, data = h.post("session", {"initData": h.init(), "sdp": fx.OFFER, "conversation_id": cid})
    assert status == 503 and fx.SYNTHETIC_TOKEN not in json.dumps(data)
    state = json.loads(h.history.get_meta(f"hermes_talk:conversation:{cid}"))
    assert state["voice_sessions"] == {} and state["activity_cursor"] is None


@check
def stale_voice_sessions_expire_without_resetting_the_conversation(h):
    data, _ = h.call()
    cid, vs = data["conversation_id"], data["voice_session_id"]
    h.clock.now += h.config.voice_session_ttl_s + 1
    assert h.ok("bootstrap", {})["conversation_id"] == cid
    state = json.loads(h.history.get_meta(f"hermes_talk:conversation:{cid}"))
    assert state["voice_sessions"][vs]["end_reason"] == "expired"
    assert h.ok("end", {"conversation_id": cid, "voice_session_id": vs}) == {"ok": True}
    assert json.loads(h.history.get_meta(f"hermes_talk:conversation:{cid}"))["voice_sessions"][vs]["end_reason"] \
        == "expired", "a later end does not rewrite an expiry"


@check
def logs_and_errors_carry_no_private_text(h):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    server.logger.addHandler(handler)
    server.logger.setLevel(logging.INFO)
    try:
        data, _ = h.call()
        h.ok("events", {"conversation_id": data["conversation_id"], "voice_session_id": data["voice_session_id"],
                        "events": [ev("u1", 0, "user", "private-utterance-marker")]})
        h.post("events", {"initData": h.init(), "conversation_id": data["conversation_id"],
                          "voice_session_id": data["voice_session_id"],
                          "events": [ev("u2", 1, "tool", "tool-dump-marker")]})
    finally:
        server.logger.removeHandler(handler)
    text = stream.getvalue()
    assert "api/events 200" in text.replace("/tg-voice/", "")
    for private in ("private-utterance-marker", "tool-dump-marker", fx.SOUL_MARKER, fx.SKILL_MARKER, "v=0",
                    "hash=", fx.BOT_TOKEN, fx.SYNTHETIC_TOKEN, "Bearer"):
        assert private not in text, private


@check
def a_deleted_native_session_is_reconciled_to_a_new_conversation(h):
    data, _ = h.call()
    gone = data["conversation_id"]
    if h.native:
        h.history.db.delete_session(gone)
    else:
        del h.history.sessions[gone]
    fresh = h.ok("bootstrap", {})["conversation_id"]
    assert fresh != gone and h.history.session_exists(fresh)
    status, err = h.post("session", {"initData": h.init(), "sdp": fx.OFFER, "conversation_id": gone})
    assert status == 409 and err["error"], (status, err)


@check
def store_failure_is_a_json_500_without_details(h):
    data, _ = h.call()

    def broken(*args, **kwargs):
        raise RuntimeError("private-store-detail-marker")
    h.history.recent = broken
    h.history.append = broken
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    server.logger.addHandler(handler)
    try:
        status, err = h.post("session", {"initData": h.init(), "sdp": fx.OFFER})
        assert status == 500 and set(err) == {"error"} and "private" not in err["error"], (status, err)
        status, err = h.post("events", {"initData": h.init(), "conversation_id": data["conversation_id"],
                                        "voice_session_id": data["voice_session_id"],
                                        "events": [ev("u1", 0, "user", "hello")]})
        assert status == 500 and set(err) == {"error"}, (status, err)
    finally:
        server.logger.removeHandler(handler)
    assert "RuntimeError" in stream.getvalue() and "private-store-detail-marker" not in stream.getvalue()
    assert len(h.fake.posts) == 1, "no upstream call when context cannot be built"


def plugin_registers_in_a_disposable_home():
    home = Path(tempfile.mkdtemp(prefix="talk-plugin-"))
    try:
        target = home / "plugins" / "hermes-talk"
        target.mkdir(parents=True)
        for name in PLUGIN_FILES:
            src = ROOT / name
            (shutil.copytree if src.is_dir() else shutil.copy2)(src, target / name)
        (home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-talk\n")
        os.environ["HERMES_HOME"] = str(home)
        from hermes_cli.plugins import PluginManager
        manager = PluginManager()
        manager.discover_and_load(force=True)
        listed = {p["name"]: p for p in manager.list_plugins()}
        assert "hermes-talk" in listed, sorted(listed)
        entry = listed["hermes-talk"]
        assert entry.get("enabled") and not entry.get("error"), entry
        assert "talk" in manager._cli_commands, sorted(manager._cli_commands)
        assert manager._cli_commands["talk"]["plugin"] == "hermes-talk"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def cli_serves_talk_in_a_disposable_profile():
    """`hermes talk serve` from the installed plugin, over HTTP. /api/session is not called here:
    resolving credentials could reach real sign-in state, so that path stays with the fakes above."""
    import socket
    import time
    root = Path(tempfile.mkdtemp(prefix="talk-cli-"))
    home = fx.write_home(root / "profiles" / "synthetic")
    proc = None
    try:
        target = home / "plugins" / "hermes-talk"
        target.mkdir(parents=True)
        for name in PLUGIN_FILES:
            src = ROOT / name
            (shutil.copytree if src.is_dir() else shutil.copy2)(src, target / name)
        (home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-talk\n")
        (home / "talk.yaml").write_text(json.dumps(fx.config_data()))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = {**os.environ, "HERMES_HOME": str(home), "TG_VOICE_BOT_TOKEN": fx.BOT_TOKEN,
               "TG_VOICE_ALLOWED_USER_IDS": str(fx.USER_ID), "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen([str(Path(sys.executable).parent / "hermes"), "talk", "serve",
                                 "--config", str(home / "talk.yaml"), "--origin", fx.ORIGIN, "--port", str(port)],
                                cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 60
        while time.time() < deadline and proc.poll() is None:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.3)
        assert proc.poll() is None, proc.stderr.read().decode()[-600:]

        def post(route, body):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            conn.request("POST", f"/tg-voice/api/{route}", body=json.dumps(body).encode(),
                         headers={"Content-Type": "application/json", "Origin": fx.ORIGIN})
            resp = conn.getresponse()
            out = resp.status, json.loads(resp.read())
            conn.close()
            return out

        init = fx.sign_init_data(fx.USER_ID, int(time.time()))
        status, boot = post("bootstrap", {"initData": init})
        assert status == 200 and boot["profile"]["id"] == "synthetic", (status, boot)
        assert post("bootstrap", {"initData": fx.sign_init_data(fx.OTHER_USER_ID, int(time.time()))})[0] == 403
        status, fresh = post("new", {"initData": init, "conversation_id": boot["conversation_id"]})
        assert status == 200 and fresh["conversation_id"] != boot["conversation_id"]
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        for cid in (boot["conversation_id"], fresh["conversation_id"]):
            row = db.get_session(cid)
            assert row["source"] == "telegram-voice" and row["title"].startswith("Telegram Voice"), row
    finally:
        db.close()
        shutil.rmtree(root, ignore_errors=True)


def run_talk(native):
    failures = 0
    label = "host" if native else "memory"
    for fn in CHECKS:
        h = Harness(native)
        try:
            fn(h)
            print(f"PASS [{label}] {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL [{label}] {fn.__name__}: {type(exc).__name__}: {exc}")
        finally:
            h.close()
    if native:
        for fn in (plugin_registers_in_a_disposable_home, cli_serves_talk_in_a_disposable_profile):
            try:
                fn()
                print(f"PASS [host] {fn.__name__}")
            except Exception as exc:
                failures += 1
                print(f"FAIL [host] {fn.__name__}: {type(exc).__name__}: {exc}")
    total = len(CHECKS) + (2 if native else 0)
    print(f"{total - failures}/{total} talk checks passed [{label}]")
    return failures


def hermes_python():
    explicit = os.environ.get("HERMES_TALK_HERMES_PYTHON")
    if explicit:
        return explicit
    found = sorted(glob.glob(os.path.expanduser("~/.hermes/installs/*/environments/*/venv/bin/python")))
    return found[0] if found else None


def run_offline():
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    legacy = subprocess.run([sys.executable, str(ROOT / "verify.py"), "--offline"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=300)
    print(legacy.stdout.strip().splitlines()[-1] if legacy.stdout.strip() else legacy.stderr[-400:])
    legacy_ok = legacy.returncode == 0 and "26/26 offline checks passed" in legacy.stdout
    memory_failures = run_talk(native=False)
    runtime = hermes_python()
    if not runtime:
        print("SKIPPED host integration: no Hermes runtime found; set HERMES_TALK_HERMES_PYTHON")
        host_ok = True
    else:
        scratch = tempfile.mkdtemp(prefix="talk-host-")
        try:
            host = subprocess.run([runtime, str(ROOT / "verify-backend.py"), "--host"], cwd=scratch,
                                  env={**env, "HERMES_HOME": scratch}, capture_output=True, text=True, timeout=600)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        print(host.stdout.strip() or f"host run produced no output: {host.stderr[-800:]}")
        host_ok = host.returncode == 0
    ok = legacy_ok and memory_failures == 0 and host_ok
    print("BACKEND OK" if ok else "BACKEND FAILED")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--host", action="store_true", help="internal: run under a Hermes runtime")
    args = parser.parse_args()
    if args.host:
        return 1 if run_talk(native=True) else 0
    return run_offline()


if __name__ == "__main__":
    sys.exit(main())
