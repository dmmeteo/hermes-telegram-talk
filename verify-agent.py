#!/usr/bin/env python3
"""Hermes Telegram Talk agent gate: voice tasks through the real Hermes agent, with synthetic data only.

  python3 verify-agent.py --require-hermes [--receipts DIR]
  python3 verify-agent.py --browser OUT_DIR    Chromium delegation job (real app.js, server and tasks)
  python3 verify-agent.py --rpc-target SDK [--receipts DIR]   gateway-RPC transport against a pinned core source

Each check runs the real Talk server over HTTP against a disposable HERMES_HOME. Tasks run as real
`hermes chat` processes from the Hermes runtime (HERMES_TALK_HERMES_PYTHON, else the first
~/.hermes/installs/*/environments/*/venv/bin/python): Hermes' own AIAgent, tool registry, tool
execution, approval policy and SessionDB. Only the inference provider is replaced, by the labelled
scripted model in tests/agent/scripted_model.py. Nothing calls a paid or live provider. Without a
runtime the gate prints SKIPPED, and --require-hermes makes that a failure.

--rpc-target runs the same Talk server against a pinned Hermes core source tree (put first on the
runtime's path) whose public hermes_cli.gateway_client selects the gateway-RPC transport. Each check's
disposable home gets its own real gateway daemon, started by that client and reaped afterwards.
"""
import argparse
import glob
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "agent"))
import server  # noqa: E402
from hermes_talk import fixtures as fx  # noqa: E402
from hermes_talk.config import parse_config  # noqa: E402

NO_TASK_USER = 1003
BLACKHOLE = "http://127.0.0.1:9"
TELEGRAM = None
PLUGIN_FILES = ("plugin.yaml", "__init__.py", "server.py", "hermes_talk", "static", "LICENSES")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


def model_config(model, extra=""):
    return (f"model:\n  default: scripted-deterministic-model\n  provider: custom\n  base_url: {model.base_url}\n"
            f"  api_key: synthetic-local-key\n{extra}")


def talk_data(workdir, profile="synthetic", receipts=None, **tasks):
    data = fx.config_data(profile={"id": profile, "display_name": "Synthetic Talk"},
                          tasks={"enabled": True, "user_ids": [fx.USER_ID, fx.OTHER_USER_ID],
                                 "toolsets": ["file", "terminal"], "workdir": str(workdir),
                                 "run_budget_seconds": 120, **tasks})
    if receipts is not None:
        data["receipts"] = receipts
    return data


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def request(port, route, body, origin=fx.ORIGIN, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = raw if raw is not None else json.dumps(body).encode()
    conn.request("POST", "/tg-voice/api/" + route, body=data,
                 headers={"Content-Type": "application/json", "Origin": origin})
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(payload or b"{}")
    except ValueError:
        return resp.status, {}


def init(user=fx.USER_ID):
    return fx.sign_init_data(user, int(time.time()))


class Env:
    """One disposable profile with the real Talk runtime and server; root may be reused to restart."""

    def __init__(self, model, root=None, profile="synthetic", profile_extra="", agent=None, **tasks):
        self.model = model
        self.root = Path(root or tempfile.mkdtemp(prefix="talk-agent-"))
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        if not (self.home / "config.yaml").exists():
            fx.write_home(self.home)
            (self.home / "config.yaml").write_text(model_config(model, profile_extra))
        os.environ["HERMES_HOME"] = str(self.home)
        from hermes_talk.history import NativeHistory
        from hermes_talk.talk import TalkRuntime
        self.config = parse_config(talk_data(self.work, profile, **tasks), self.home)
        self.history = NativeHistory(self.home)
        self.talk = TalkRuntime(self.config, fx.BOT_TOKEN, self.history, home=self.home, agent=agent)
        self.desk = self.talk.desk
        self.fake = fx.FakeUpstream()
        self.app = server.VoiceApp(fx.BOT_TOKEN, {fx.USER_ID, fx.OTHER_USER_ID, NO_TASK_USER}, fx.ORIGIN,
                                   resolve=self.fake.resolve, post=self.fake.post, talk=self.talk)
        self.app.limiter.limit = 1000
        for limiter in self.app.talk_limiters.values():
            limiter.limit = 1000
        self.httpd = server.BoundedServer(("127.0.0.1", 0), server.make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def post(self, route, fields, user=fx.USER_ID, **kw):
        return request(self.port, route, {"initData": init(user), **fields}, **kw)

    def call(self, user=fx.USER_ID):
        status, boot = self.post("bootstrap", {}, user)
        assert status == 200, boot
        status, answer = self.post("session", {"sdp": fx.OFFER, "conversation_id": boot["conversation_id"]}, user)
        assert status == 200, answer
        return boot["conversation_id"], answer["voice_session_id"]

    def instructions(self):
        return self.fake.posts[-1][2]["session"]["instructions"]

    def task(self, conv, vs, text, delegation=None, user=fx.USER_ID):
        return self.post("task", {"conversation_id": conv, "voice_session_id": vs,
                                  "delegation_item_id": delegation or "del_" + secrets.token_hex(6), "request": text}, user)

    def jobs(self, conv, vs, user=fx.USER_ID):
        status, body = self.post("tasks", {"conversation_id": conv, "voice_session_id": vs}, user)
        assert status == 200, body
        return {j["id"]: j for j in body["jobs"]}, body

    def wait(self, job_id, conv, vs, states=TERMINAL, timeout=150):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.jobs(conv, vs)[0][job_id]
            if job["state"] in states:
                return job
            time.sleep(0.3)
        raise AssertionError(f"job {job_id} did not reach {states}: {job}")

    def events(self, conv, vs, *turns, user=fx.USER_ID):
        events = [{"id": "e" + secrets.token_hex(6), "sequence": i, "role": role, "text": text}
                  for i, (role, text) in enumerate(turns)]
        status, body = self.post("events", {"conversation_id": conv, "voice_session_id": vs, "events": events}, user)
        assert status == 200, body

    def native(self, job_id):
        return self.talk.board.jobs[job_id]

    def close(self, keep=False):
        board = self.talk.board
        if board is not None and not keep:
            for job in list(board.jobs.values()):
                if job.state == "running":
                    board.agent.stop(job) if board.gateway else board.agent.stop(job.pid, job.pid_start)
            board.wait_idle(20)
        if board is not None and board.gateway:
            board.agent.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.history.close()
        if not keep:
            reap_gateways(self.root)
            shutil.rmtree(self.root, ignore_errors=True)


def gateway_pids(root):
    """Gateway daemons a test home under root started (none outside the gateway-RPC mode)."""
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().split(b"\0")
            env = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        home = next((v[12:].decode(errors="replace") for v in env if v.startswith(b"HERMES_HOME=")), "")
        if b"gateway" in cmd and home.startswith(str(root)):
            found.append(int(proc.name))
    return found


def reap_gateways(root):
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        pids = gateway_pids(root)
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.time() + grace
        while pids and time.time() < deadline:
            pids = gateway_pids(root)
            time.sleep(0.2)
        if not pids:
            return


def requests_with(model, text):
    return [m for m in model.requests if any(text in str(x.get("content")) for x in m if x.get("role") == "user")]


def native_tool_roundtrip(model):
    e = Env(model)
    try:
        conv, vs = e.call()
        assert "You can hand work to Hermes" in e.instructions()
        marker = "FIXTURE-" + secrets.token_hex(8)
        fixture = e.work / "fixture.txt"
        fixture.write_text(marker + "\n")
        status, body = e.task(conv, vs, f"read {fixture}", "del_tool_1")
        assert status == 200 and body["created"] and body["channel"] == "commentary", body
        job = e.wait(body["job"]["id"], conv, vs)
        assert job["state"] == "succeeded" and marker in job["result"], job
        native = e.native(job["id"])
        rows = e.history.db.get_messages(native.native_session_id)
        tool_rows = [r for r in rows if r["role"] == "tool" and marker in (r.get("content") or "")]
        assert tool_rows, "the native session stored the real read_file result"
        assert e.history.db.get_session(native.native_session_id)["source"] == "telegram-voice"
        assert not Path(native.output).exists(), "the per-job output file is removed once the job settles"
        return {"marker": marker, "job": job["id"], "native_session": native.native_session_id,
                "tool_rows": len(tool_rows), "result": job["result"][:200], "model": "scripted-deterministic-model"}
    finally:
        e.close()


def background_result_delivery(model):
    e = Env(model)
    try:
        conv, vs = e.call()
        status, body = e.task(conv, vs, "slow 4", "del_bg_1")
        job_id = body["job"]["id"]
        started = time.time()
        e.events(conv, vs, ("user", "still talking"), ("assistant", "I am still here"))
        assert e.post("bootstrap", {})[0] == 200
        responsive = time.time() - started
        assert responsive < 3, f"voice routes answered in {responsive:.1f}s while the task ran"
        assert e.jobs(conv, vs)[0][job_id]["state"] in ("accepted", "running")
        job = e.wait(job_id, conv, vs)
        assert job["state"] == "succeeded" and job["result"] == "SCRIPTED-SLOW-DONE" and job["receipt"] == "undelivered"
        assert job["delegation_item_id"] == "del_bg_1" and job["voice_session_id"] == vs
        _, vs2 = e.call()
        status, wrong = e.post("task/ack", {"conversation_id": conv, "voice_session_id": vs2, "job_id": job_id})
        assert status == 409, wrong
        status, acked = e.post("task/ack", {"conversation_id": conv, "voice_session_id": vs, "job_id": job_id})
        assert status == 200 and acked["job"]["receipt"] == "delivered", acked
        assert e.native(job_id).delivered_via == "delegation"
        return {"job": job_id, "delegation_item_id": "del_bg_1", "voice_routes_seconds": round(responsive, 2),
                "foreign_call_ack": 409, "receipt": "delivered", "delivered_via": "delegation"}
    finally:
        e.close()


def context_update_injection(model):
    e = Env(model)
    try:
        conv, vs1 = e.call()
        _, body = e.task(conv, vs1, "slow 5", "del_ctx_1")
        job_id = body["job"]["id"]
        assert e.post("end", {"conversation_id": conv, "voice_session_id": vs1})[0] == 200
        _, vs2 = e.call()
        assert f"Hermes task {body['job']['number']}" in e.instructions() and "is running" in e.instructions()
        job = e.wait(job_id, conv, vs2)
        assert job["state"] == "succeeded" and job["receipt"] == "undelivered"
        assert e.jobs(conv, vs2)[1]["context_pending"] is True
        pulse = e.history.create_session("Synthetic mid-call update", "cli")
        e.history.append(pulse, [{"role": "assistant", "content": "synthetic done", "platform_message_id": "p1",
                                  "timestamp": time.time()}])
        status, update = e.post("context", {"conversation_id": conv, "voice_session_id": vs2})
        assert status == 200 and update["revision"] == 1, update
        assert "SCRIPTED-SLOW-DONE" in update["text"] and "Synthetic mid-call update" in update["text"], update
        assert update["sources"] == {"jobs": 1, "activity": 1}, update
        assert e.native(job_id).receipt == "undelivered", "nothing is delivered before the client acks"
        status, _ = e.post("context/ack", {"conversation_id": conv, "voice_session_id": vs2, "revision": 1})
        assert status == 200 and e.native(job_id).delivered_via == "context"
        status, again = e.post("context", {"conversation_id": conv, "voice_session_id": vs2})
        assert status == 200 and again["revision"] is None and again["text"] == "", again
        assert e.post("context/ack", {"conversation_id": conv, "voice_session_id": vs2, "revision": 99})[0] == 404
        return {"job": job_id, "revision": 1, "sources": update["sources"], "delivered_via": "context",
                "second_refresh": "empty"}
    finally:
        e.close()


def persistent_two_call_history(model):
    e = Env(model)
    root = e.root
    try:
        conv, vs1 = e.call()
        _, first = e.task(conv, vs1, "first task alpha", "del_h1")
        job1 = e.wait(first["job"]["id"], conv, vs1)
        e.events(conv, vs1, ("user", "alpha question"), ("assistant", "alpha answer"))
        assert e.post("end", {"conversation_id": conv, "voice_session_id": vs1})[0] == 200
        session1 = e.native(job1["id"]).native_session_id
        e.close(keep=True)
        e = Env(model, root=root)
        conv2, vs2 = e.call()
        assert conv2 == conv, "the account's conversation survives a service restart"
        text = e.instructions()
        assert text.index("user: alpha question") < text.index("assistant: alpha answer"), text
        e.events(conv, vs2, ("user", "recall"))
        _, second = e.task(conv, vs2, "recall", "del_h2")
        job2 = e.wait(second["job"]["id"], conv, vs2)
        assert job2["state"] == "succeeded", job2
        prompt = [m for m in requests_with(model, "TASK: recall")][-1]
        user_text = [x["content"] for x in prompt if x.get("role") == "user"][-1]
        assert user_text.count("recall") == 1, user_text
        assert user_text.index("alpha question") < user_text.index("alpha answer"), user_text
        assert e.native(job2["id"]).native_session_id == session1, "tasks continue the conversation's native session"
        assert "SCRIPTED-ANSWER: first task alpha" in job2["result"], job2["result"]
        return {"conversation": conv, "restarted": True, "native_session": session1,
                "request_occurrences": 1, "agent_recall": job2["result"][:300]}
    finally:
        e.close()


def pulse_visibility_and_cursor(model):
    e = Env(model)
    try:
        before = e.history.create_session("Synthetic owner build", "cli")
        e.history.append(before, [{"role": "assistant", "content": "built", "platform_message_id": "b1",
                                   "timestamp": time.time()}])
        # Accepted asynchronous startup (612eac4): a call opens on the account's PREPARED Pulse state and never
        # waits for a scan. The session above predates this call, so the account's background preparation (the
        # engine tick or an earlier visit) is let finish first; the late-arrival path is pulse_late_after_fast_start.
        e.talk.engine.prepare_now(fx.USER_ID)
        conv, vs = e.call()
        assert re.search(r"^- P1 \[active\] Synthetic owner build \(cli session ", e.instructions(), re.M)
        card = lambda title: next(c for c in e.talk.pulse.cards(e.talk.account(fx.USER_ID)) if c["title"] == title)
        assert card("Synthetic owner build")["state"] == "exposed", "supplied to the model is not heard"
        e.events(conv, vs, ("assistant", "hello"), ("user", "hi"))
        assert card("Synthetic owner build")["state"] == "exposed", "a greeting consumed the briefing"
        e.events(conv, vs, ("assistant", "hello again"), ("user", "hi"), ("assistant", "Synthetic owner build is built."),
                 ("user", "great"))
        spoke = card("Synthetic owner build")
        assert spoke["state"] == "acknowledged" and spoke["acknowledged"]["call"] == vs, spoke
        later = e.history.create_session("Synthetic later deploy", "cli")
        e.history.append(later, [{"role": "assistant", "content": "deployed", "platform_message_id": "l1",
                                  "timestamp": time.time() + 1}])
        status, update = e.post("context", {"conversation_id": conv, "voice_session_id": vs})
        assert status == 200 and "Synthetic later deploy" in update["text"] and "Synthetic owner build" not in update["text"]
        assert card("Synthetic later deploy")["state"] == "candidate", "an unacked update exposes nothing"
        e.post("context/ack", {"conversation_id": conv, "voice_session_id": vs, "revision": update["revision"]})
        assert card("Synthetic later deploy")["state"] == "exposed", "appended is exposed, not heard"
        _, vs2 = e.call()
        brief = e.instructions().split("## Activity since the last visit\n", 1)[1].split("\n\n## ", 1)[0]
        assert "Synthetic owner build" not in brief and "Synthetic later deploy" in brief, brief
        return {"briefed_at_open": before, "after_greeting": "exposed", "acknowledged": spoke["acknowledged"]["by"],
                "delivered_mid_call": later, "unheard_update_offered_again": True}
    finally:
        e.close()


def pulse_late_after_fast_start(model):
    """The first call opens without waiting; activity read later arrives in that same call as a context update."""
    e = Env(model)
    try:
        before = e.history.create_session("Synthetic late build", "cli")
        e.history.append(before, [{"role": "assistant", "content": "built late", "platform_message_id": "lb1",
                                   "timestamp": time.time()}])
        started = time.monotonic()
        conv, vs = e.call()
        opened_s = time.monotonic() - started
        first = e.instructions()
        assert "still being read" in first and "Synthetic late build" not in first, first[-800:]
        e.talk.engine.prepare_now(fx.USER_ID)  # the background preparation finishes after the call opened
        card = lambda: next(c for c in e.talk.pulse.cards(e.talk.account(fx.USER_ID)) if c["title"] == "Synthetic late build")
        status, update = e.post("context", {"conversation_id": conv, "voice_session_id": vs})
        assert status == 200 and update["revision"] and "Synthetic late build" in update["text"], update
        assert card()["state"] == "candidate", "an unacked update exposes nothing"
        e.post("context/ack", {"conversation_id": conv, "voice_session_id": vs, "revision": update["revision"]})
        assert card()["state"] == "exposed", "appended is exposed, not heard"
        assert card()["ref"] == before, (card()["ref"], before)  # provenance: the exact source session
        status, again = e.post("context", {"conversation_id": conv, "voice_session_id": vs})
        assert status == 200 and "Synthetic late build" not in (again.get("text") or ""), "delivered twice"
        return {"opened_s": round(opened_s, 3), "late_revision": update["revision"], "provenance": card()["ref"]}
    finally:
        e.close()


def cross_user_denial(model):
    e = Env(model)
    try:
        conv, vs = e.call()
        _, body = e.task(conv, vs, "slow 2", "del_x1")
        job_id = body["job"]["id"]
        conv_b, vs_b = e.call(fx.OTHER_USER_ID)
        u2 = fx.OTHER_USER_ID
        denied = {
            "ack_foreign_conversation": e.post("task/ack", {"conversation_id": conv, "voice_session_id": vs,
                                                            "job_id": job_id}, u2)[0],
            "ack_guessed_job": e.post("task/ack", {"conversation_id": conv_b, "voice_session_id": vs_b,
                                                   "job_id": job_id}, u2)[0],
            "cancel_guessed_job": e.post("task/cancel", {"conversation_id": conv_b, "job_id": job_id}, u2)[0],
            "cancel_foreign_conversation": e.post("task/cancel", {"conversation_id": conv, "job_id": job_id}, u2)[0],
            "list_foreign_conversation": e.post("tasks", {"conversation_id": conv, "voice_session_id": vs}, u2)[0],
            "task_in_foreign_call": e.task(conv, vs, "read /etc/hostname", "del_x2", u2)[0],
        }
        assert set(denied.values()) == {404}, denied
        assert e.wait(job_id, conv, vs)["state"] == "succeeded", "the owner's job is untouched"
        assert len(e.talk.board.jobs) == 1
        status, boot = e.post("bootstrap", {}, NO_TASK_USER)
        assert status == 200 and boot["tasks"] == {"available": False}
        conv_c, vs_c = e.call(NO_TASK_USER)
        status, refused = e.task(conv_c, vs_c, "read /etc/hostname", "del_x3", NO_TASK_USER)
        assert status == 503 and len(e.talk.board.jobs) == 1, refused
        return {"denied": denied, "non_task_user": status}
    finally:
        e.close()


def cross_profile_denial(model):
    a = Env(model)
    b = Env(model, profile="other")
    try:
        conv, vs = a.call()
        _, body = a.task(conv, vs, "slow 1", "del_p1")
        job_id = body["job"]["id"]
        a.wait(job_id, conv, vs)
        conv_b, vs_b = b.call()
        other = b.post("task/ack", {"conversation_id": conv_b, "voice_session_id": vs_b, "job_id": job_id})[0]
        other_conv = b.post("tasks", {"conversation_id": conv, "voice_session_id": vs})[0]
        job = a.native(job_id)
        forged_id = "jb_" + secrets.token_urlsafe(18)
        from hermes_talk import jobs
        from dataclasses import replace
        a.history.set_meta(jobs.job_key(forged_id), jobs.dump(replace(job, id=forged_id,
                                                                     scope=job.scope.replace("synthetic:", "other:"))))
        root = a.root
        a.close(keep=True)
        a = Env(model, root=root)
        assert forged_id in a.talk.board.jobs
        forged = {"ack": a.post("task/ack", {"conversation_id": conv, "voice_session_id": vs, "job_id": forged_id})[0],
                  "cancel": a.post("task/cancel", {"conversation_id": conv, "job_id": forged_id})[0]}
        assert other == 404 and other_conv == 404 and set(forged.values()) == {404}, (other, other_conv, forged)
        return {"other_profile_ack": other, "other_profile_list": other_conv, "forged_scope": forged}
    finally:
        a.close()
        b.close()


def serve(root, port, env):
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--config", str(root / "talk.yaml"),
                             "--origin", fx.ORIGIN, "--port", str(port)], cwd=root, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 60
    while time.time() < deadline and proc.poll() is None:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return proc
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"server did not start: {proc.stderr.read().decode()[-800:] if proc.poll() is not None else 'timeout'}")


def stored_job(home, job_id):
    from hermes_state import SessionDB
    from hermes_talk import jobs
    db = SessionDB(db_path=home / "state.db")
    try:
        return jobs.load(db.get_meta(jobs.job_key(job_id)))
    finally:
        db.close()


def prepare_call(home, data):
    """A live voice session made through the Talk store, so the served process never calls a provider."""
    os.environ["HERMES_HOME"] = str(home)
    from hermes_talk.history import NativeHistory
    from hermes_talk.store import TalkStore, VoiceScope
    history = NativeHistory(home)
    try:
        store = TalkStore(history, time.time, 7200, 1200)
        scope = VoiceScope(data["profile"]["id"], fx.BOT_TOKEN.split(":")[0], fx.USER_ID)
        conv, _ = store.current(scope)
        return conv, store.start_voice_session(scope, conv, None)
    finally:
        history.close()


def restart_job_reconciliation(model):
    root = Path(tempfile.mkdtemp(prefix="talk-restart-"))
    home, work = root / "profiles" / "synthetic", root / "work"
    work.mkdir(parents=True)
    fx.write_home(home)
    (home / "config.yaml").write_text(model_config(model))
    # Reports are checked in-process against the synthetic transport; this served process sends none.
    data = talk_data(work, receipts={"enabled": False})
    (root / "talk.yaml").write_text(json.dumps(data))
    conv, vs = prepare_call(home, data)
    env = {**os.environ, "HERMES_HOME": str(home), "TG_VOICE_BOT_TOKEN": fx.BOT_TOKEN, "TELEGRAM_PROXY": BLACKHOLE,
           "TG_VOICE_ALLOWED_USER_IDS": f"{fx.USER_ID},{fx.OTHER_USER_ID}"}
    port = free_port()
    proc = serve(root, port, env)
    procs = [proc]
    call = {"initData": init(), "conversation_id": conv, "voice_session_id": vs}
    try:
        status, body = request(port, "task", {**call, "delegation_item_id": "del_r1", "request": "slow 8"})
        assert status == 200, body
        adopted_id = body["job"]["id"]
        deadline = time.time() + 60
        while not requests_with(model, "TASK: slow 8") and time.time() < deadline:
            time.sleep(0.2)
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
        running = stored_job(home, adopted_id)
        assert running.state == "running" and running.pid, running
        proc = serve(root, port, env)
        procs.append(proc)
        deadline = time.time() + 90
        while time.time() < deadline:
            job = {j["id"]: j for j in request(port, "tasks", {**call, "initData": init()})[1]["jobs"]}[adopted_id]
            if job["state"] in TERMINAL:
                break
            time.sleep(0.3)
        adopted = stored_job(home, adopted_id)
        assert adopted.state == "succeeded" and "adopted after restart" in adopted.notes, adopted
        second = subprocess.run([sys.executable, str(ROOT / "server.py"), "--config", str(root / "talk.yaml"),
                                 "--origin", fx.ORIGIN, "--port", str(free_port())], cwd=root, env=env,
                                capture_output=True, text=True, timeout=60)
        assert second.returncode != 0 and "another Talk server already serves this profile" in second.stderr, \
            second.stderr[-400:]
        assert stored_job(home, adopted_id) == adopted, "the refused second server recovered nothing"
        assert len(requests_with(model, "TASK: slow 8")) == 1, "the adopted run was not started twice"

        status, body = request(port, "task", {**call, "initData": init(), "delegation_item_id": "del_r2",
                                              "request": "slow 60"})
        lost_id = body["job"]["id"]
        deadline = time.time() + 60
        while not requests_with(model, "TASK: slow 60") and time.time() < deadline:
            time.sleep(0.2)
        lost = stored_job(home, lost_id)
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
        os.killpg(lost.pid, signal.SIGKILL)
        time.sleep(0.5)
        proc = serve(root, port, env)
        procs.append(proc)
        job = {j["id"]: j for j in request(port, "tasks", {**call, "initData": init()})[1]["jobs"]}[lost_id]
        assert job["state"] == "interrupted" and "not rerun" in job["error"], job
        time.sleep(2)
        assert len(requests_with(model, "TASK: slow 60")) == 1, "an interrupted job is never rerun"
        return {"adopted": adopted_id, "adopted_state": adopted.state, "interrupted": lost_id,
                "second_server_on_profile": "refused before recovery",
                "interrupted_error": job["error"], "server_restarts": 2}
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                p.wait(10)
        shutil.rmtree(root, ignore_errors=True)


def voice_disconnect_vs_cancel(model):
    e = Env(model)
    try:
        conv, vs = e.call()
        _, kept = e.task(conv, vs, "slow 4", "del_d1")
        assert e.post("end", {"conversation_id": conv, "voice_session_id": vs})[0] == 200
        _, vs2 = e.call()
        kept_job = e.wait(kept["job"]["id"], conv, vs2)
        assert kept_job["state"] == "succeeded", "ending the call cancels nothing"
        _, doomed = e.task(conv, vs2, "slow 90", "del_d2")
        job_id = doomed["job"]["id"]
        e.wait(job_id, conv, vs2, states=("running",))
        deadline = time.time() + 30
        while not e.native(job_id).pid and time.time() < deadline:
            time.sleep(0.1)
        from hermes_talk.agent import Readiness
        ready = e.talk.agent.readiness
        e.talk.agent.readiness = lambda: Readiness(False, "synthetic: readiness lapsed")
        status, cancelled = e.post("task/cancel", {"conversation_id": conv, "job_id": job_id})
        assert status == 200, f"cancel must not depend on readiness: {cancelled}"
        e.talk.agent.readiness = ready
        job = e.wait(job_id, conv, vs2, timeout=40)
        native = e.native(job_id)
        from hermes_talk.agent import alive
        assert job["state"] == "cancelled" and not alive(native.pid, native.pid_start), job
        again = e.post("task/cancel", {"conversation_id": conv, "job_id": job_id})[1]["job"]
        assert again["state"] == "cancelled"
        _, voiced = e.task(conv, vs2, "slow 90", "del_d3")
        e.wait(voiced["job"]["id"], conv, vs2, states=("running",))
        status, spoken = e.task(conv, vs2, f"cancel task {voiced['job']['number']}", "del_d4")
        assert status == 200 and spoken["channel"] == "speakable" and not spoken["created"], spoken
        assert e.wait(voiced["job"]["id"], conv, vs2, timeout=40)["state"] == "cancelled"
        return {"kept_after_end": kept_job["state"], "cancelled": job_id, "voice_cancel": voiced["job"]["id"]}
    finally:
        e.close()


def delegation_id_dedup(model):
    e = Env(model)
    try:
        conv, vs = e.call()
        unique = "dedupe " + secrets.token_hex(6)
        first = e.task(conv, vs, unique, "del_same")[1]
        second = e.task(conv, vs, unique, "del_same")[1]
        assert first["created"] and not second["created"] and first["job"]["id"] == second["job"]["id"]
        e.wait(first["job"]["id"], conv, vs)
        third = e.task(conv, vs, unique, "del_same")[1]
        assert third["job"]["id"] == first["job"]["id"] and "already sent" in third["reply"]
        assert len(requests_with(model, "TASK: " + unique)) == 1, "one delegation id runs once"
        _, vs2 = e.call()
        other_call = e.task(conv, vs2, unique, "del_same")[1]
        assert other_call["created"] and other_call["job"]["id"] != first["job"]["id"]
        e.wait(other_call["job"]["id"], conv, vs2)
        return {"job": first["job"]["id"], "repeats": 2, "native_runs": 1, "new_call_job": other_call["job"]["id"]}
    finally:
        e.close()


def plugin_native_install(model):
    root = Path(tempfile.mkdtemp(prefix="talk-agent-install-"))
    user_home = root / "home"
    user_home.mkdir()
    hermes = str(Path(sys.executable).parent / "hermes")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(user_home), "PYTHONDONTWRITEBYTECODE": "1",
           "TELEGRAM_PROXY": BLACKHOLE}
    native = lambda *args: subprocess.run([hermes, *args], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    proc = None
    profile = user_home / ".hermes" / "profiles" / "synthetic"
    target = profile / "plugins" / "hermes-telegram-talk"
    try:
        out = native("profile", "create", "synthetic", "--no-alias", "--no-skills")
        assert out.returncode == 0, out.stderr[-600:]
        fx.write_home(profile)
        target.mkdir(parents=True)
        for name in PLUGIN_FILES:
            src = ROOT / name
            (shutil.copytree if src.is_dir() else shutil.copy2)(src, target / name)
        for path in [target, *target.rglob("*")]:
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        (profile / "config.yaml").write_text(model_config(model, "plugins:\n  enabled:\n    - hermes-telegram-talk\n"))
        listed = [line.split() for line in native("-p", "synthetic", "plugins", "list", "--plain", "--no-bundled").stdout.splitlines()]
        assert ["enabled", "user", "0.2.0", "hermes-telegram-talk"] in listed, listed
        work = root / "work"
        work.mkdir()
        data = talk_data(work)
        (profile / "talk.yaml").write_text(json.dumps(data))
        conv, vs = prepare_call(profile, data)
        port = free_port()
        proc = subprocess.Popen([hermes, "-p", "synthetic", "talk", "serve", "--config", str(profile / "talk.yaml"),
                                 "--origin", fx.ORIGIN, "--port", str(port)], cwd=root,
                                env={**env, "TG_VOICE_BOT_TOKEN": fx.BOT_TOKEN,
                                     "TG_VOICE_ALLOWED_USER_IDS": f"{fx.USER_ID}"},
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 60
        while time.time() < deadline and proc.poll() is None:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.3)
        argv = Path(f"/proc/{proc.pid}/cmdline").read_bytes().split(b"\0")
        assert argv[1].decode() == str(target / "server.py"), argv
        status, boot = request(port, "bootstrap", {"initData": init()})
        assert status == 200 and boot["tasks"] == {"available": True}, boot
        marker = "INSTALLED-" + secrets.token_hex(8)
        (work / "installed.txt").write_text(marker)
        call = {"initData": init(), "conversation_id": conv, "voice_session_id": vs}
        status, body = request(port, "task", {**call, "delegation_item_id": "del_i1", "request": f"read {work / 'installed.txt'}"})
        assert status == 200, body
        deadline = time.time() + 120
        while time.time() < deadline:
            job = request(port, "tasks", {**call, "initData": init()})[1]["jobs"][0]
            if job["state"] in TERMINAL:
                break
            time.sleep(0.3)
        assert job["state"] == "succeeded" and marker in job["result"], job
        assert job["report"] and job["report"]["destination"] == "this Telegram chat", job
        assert not (user_home / ".hermes" / "installs").exists(), "no Hermes runtime was provisioned"
        return {"installed_server": str(target / "server.py").replace(str(root), "<disposable>"), "marker": marker,
                "job": job["id"], "tasks_available": True, "default_report": job["report"],
                "report_network": f"TELEGRAM_PROXY={BLACKHOLE}; no Telegram egress possible from this check"}
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
        for path in [target, *target.rglob("*")] if target.exists() else []:
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
        shutil.rmtree(root, ignore_errors=True)


def unauthenticated_job_denial(model):
    e = Env(model)
    try:
        conv, vs = e.call()
        routes = {"task": {"conversation_id": conv, "voice_session_id": vs, "delegation_item_id": "del_u",
                           "request": "read /etc/hostname"},
                  "tasks": {"conversation_id": conv, "voice_session_id": vs},
                  "task/cancel": {"conversation_id": conv, "job_id": "jb_" + "a" * 24},
                  "task/ack": {"conversation_id": conv, "voice_session_id": vs, "job_id": "jb_" + "a" * 24},
                  "context": {"conversation_id": conv, "voice_session_id": vs},
                  "context/ack": {"conversation_id": conv, "voice_session_id": vs, "revision": 1}}
        good = init()
        bad = good[:-1] + ("1" if good.endswith("0") else "0")
        expired = fx.sign_init_data(fx.USER_ID, int(time.time()) - 7200)
        seen = {}
        before = len(model.requests)
        for route, fields in routes.items():
            seen[route] = [request(e.port, route, {"initData": bad, **fields})[0],
                           request(e.port, route, {"initData": expired, **fields})[0],
                           request(e.port, route, {**fields})[0],
                           request(e.port, route, {"initData": good, **fields}, origin="https://evil.example")[0],
                           request(e.port, route, {"initData": fx.sign_init_data(9999, int(time.time())), **fields})[0],
                           request(e.port, route, {"initData": fx.sign_init_data(fx.USER_ID, int(time.time()), "654321:" + "x" * 30), **fields})[0]]
            assert seen[route] == [401, 401, 400, 403, 403, 401], (route, seen[route])
        assert not e.talk.board.jobs and len(model.requests) == before, "nothing was created or run"
        return {"statuses": seen, "jobs_created": 0}
    finally:
        e.close()


def limits_and_approvals(model):
    os.environ["HERMES_YOLO_MODE"] = "1"
    os.environ["TG_VOICE_BOT_TOKEN"] = fx.BOT_TOKEN
    e = Env(model, max_open_per_user=2)
    try:
        conv, vs = e.call()
        limits = {
            "request_too_long": e.task(conv, vs, "x" * 2001)[0],
            "malformed_delegation": e.task(conv, vs, "hello", "bad id!")[0],
            "extra_field": e.post("task", {"conversation_id": conv, "voice_session_id": vs, "delegation_item_id": "d",
                                           "request": "hi", "toolsets": ["terminal"]})[0],
            "body_too_large": request(e.port, "task", None, raw=b'{"initData":"' + b"a" * 40000 + b'"}')[0],
        }
        assert limits == {"request_too_long": 400, "malformed_delegation": 400, "extra_field": 400,
                          "body_too_large": 413}, limits
        open_jobs = [e.task(conv, vs, "slow 90")[1]["job"]["id"] for _ in range(2)]
        limits["open_cap"] = e.task(conv, vs, "slow 90")[0]
        assert limits["open_cap"] == 429
        for job_id in open_jobs:
            e.post("task/cancel", {"conversation_id": conv, "job_id": job_id})
        for job_id in open_jobs:
            assert e.wait(job_id, conv, vs, timeout=40)["state"] == "cancelled"
        victim = e.work / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("keep")
        _, body = e.task(conv, vs, f"shell rm -rf {victim}")
        blocked = e.wait(body["job"]["id"], conv, vs)
        assert "BLOCKED" in blocked["result"] and (victim / "keep.txt").exists(), blocked
        _, body = e.task(conv, vs, "shell printenv HERMES_HOME HERMES_YOLO_MODE TG_VOICE_BOT_TOKEN TG_VOICE_ALLOWED_USER_IDS")
        environ = e.wait(body["job"]["id"], conv, vs)
        assert environ["state"] == "succeeded" and str(e.home) in environ["result"], environ
        assert "HERMES_YOLO_MODE" not in environ["result"] and "synthetic-offline-bot-token" not in environ["result"]
        assert "TG_VOICE" not in environ["result"]
    finally:
        e.close()
        os.environ.pop("HERMES_YOLO_MODE", None)
        os.environ.pop("TG_VOICE_BOT_TOKEN", None)
    risky = Env(model, profile_extra="approvals:\n  single_query_mode: approve\n")
    try:
        conv, vs = risky.call()
        assert risky.post("bootstrap", {})[1]["tasks"] == {"available": False}
        assert "You can hand work to Hermes" not in risky.instructions(), "no greeting with unready capabilities"
        status, refused = risky.task(conv, vs, "shell echo hi")
        assert status == 503 and "auto-approves" in refused["error"] and not risky.talk.board.jobs, refused
    finally:
        risky.close()
    return {"limits": limits, "dangerous_command": "BLOCKED by native single-query approval; target kept",
            "child_env": "no HERMES_YOLO_MODE, bot token or allowlist", "auto_approving_profile": 503}


# The target profile's approvals shape: smart, with single_query_mode left at the native default (deny).
SMART_APPROVALS = "approvals:\n  mode: smart\n  timeout: 60\n  cron_mode: deny\n"
GUARDIAN_PROMPT = "You are a security reviewer for an AI coding agent."


def smart_approval_profile(model):
    # The smart classifier's auxiliary call is routed to the scripted model, so any classifier run is recorded.
    guardian = (f"auxiliary:\n  approval:\n    provider: custom\n    model: scripted-deterministic-model\n"
                f"    base_url: {model.base_url}\n    api_key: synthetic-local-key\n")
    e = Env(model, profile_extra=SMART_APPROVALS + guardian)
    config = e.home / "config.yaml"
    before = hashlib.sha256(config.read_bytes()).hexdigest()

    def guardian_calls():
        with model.lock:
            seen = model.requests + model.title_requests
        return sum(1 for messages in seen if any(GUARDIAN_PROMPT in str(m.get("content")) for m in messages))

    try:
        assert e.talk.agent.readiness().ready, e.talk.agent.readiness()
        conv, vs = e.call()
        assert e.post("bootstrap", {})[1]["tasks"] == {"available": True}, "a smart profile offers tasks"
        victim = e.work / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("keep")
        guardian_before = guardian_calls()
        _, body = e.task(conv, vs, f"shell rm -rf {victim}")
        blocked = e.wait(body["job"]["id"], conv, vs)
        assert blocked["state"] == "succeeded" and "BLOCKED" in blocked["result"], blocked
        assert "single-query mode (-q) runs without a user present" in blocked["result"], blocked
        assert (victim / "keep.txt").read_text() == "keep", "the flagged command never ran"
        assert guardian_calls() == guardian_before, "the smart classifier was not consulted"
        control = subprocess.run([sys.executable, "-c", "from tools.approval_smart import _smart_approve;"
                                  "print(_smart_approve('rm -rf /nonexistent-talk-probe', 'recursive delete'))"],
                                 env=e.talk.agent.env, cwd=str(e.work),
                                 capture_output=True, text=True, timeout=120)
        assert guardian_calls() == guardian_before + 1, ("control: a direct classifier call reaches the scripted "
                                                         f"model, so a hidden one would be seen: {control.stderr[-400:]}")
        after = hashlib.sha256(config.read_bytes()).hexdigest()
        assert after == before, "the profile config is unchanged"
    finally:
        e.close()
    refused = {}
    e = Env(model)
    try:
        conv, vs = e.call()
        for name, approvals in (("off", "mode: 'off'"), ("yaml_off", "mode: off"), ("unknown", "mode: auto"),
                                ("bool_true", "mode: true"), ("smart_sq_approve", "mode: smart\n  single_query_mode: approve"),
                                ("smart_sq_off", "mode: smart\n  single_query_mode: 'off'"), ("not_mapping", None)):
            block = "approvals: [smart]\n" if approvals is None else f"approvals:\n  {approvals}\n"
            (e.home / "config.yaml").write_text(model_config(model, block))
            readiness = e.talk.agent.readiness()
            assert not readiness.ready, (name, readiness)
            refused[name] = readiness.reason
        status, body = e.task(conv, vs, "shell echo hi")
        assert status == 503 and not e.talk.board.jobs, body
        for name, approvals in (("manual", "approvals:\n  mode: manual\n"), ("default", "")):
            (e.home / "config.yaml").write_text(model_config(model, approvals))
            assert e.talk.agent.readiness().ready, name
    finally:
        e.close()
    return {"smart_ready": True, "dangerous_single_query_denied": "BLOCKED by native single-query deny; target kept",
            "blocked_result": blocked["result"][:400], "smart_classifier_calls_during_task": 0,
            "classifier_control_calls": 1, "classifier_control_verdict": control.stdout.strip()[-40:],
            "profile_config_sha256": {"before": before, "after": after}, "refused": refused,
            "manual_and_default_ready": True, "unsafe_mode_task_status": status}


def receipt_ack_no_double_speech(model):
    e = Env(model)
    try:
        conv, vs = e.call()
        _, body = e.task(conv, vs, "receipt check", "del_rc")
        job_id = body["job"]["id"]
        e.wait(job_id, conv, vs)
        acks = [e.post("task/ack", {"conversation_id": conv, "voice_session_id": vs, "job_id": job_id}) for _ in range(3)]
        assert all(s == 200 and b["job"]["receipt"] == "delivered" for s, b in acks), acks
        native = e.native(job_id)
        assert native.acks == 3 and sum(n.startswith("delivered:") for n in native.notes) == 1, native
        _, vs2 = e.call()
        assert "SCRIPTED-ANSWER: receipt check" not in e.instructions(), "a delivered result is not offered again"
        assert e.jobs(conv, vs2)[1]["context_pending"] is False
        status, update = e.post("context", {"conversation_id": conv, "voice_session_id": vs2})
        assert "receipt check" not in update["text"]
        _, late = e.task(conv, vs2, "late result", "del_late")
        assert e.post("end", {"conversation_id": conv, "voice_session_id": vs2})[0] == 200
        e.wait(late["job"]["id"], conv, vs2)
        _, vs3 = e.call()
        assert "SCRIPTED-ANSWER: late result" in e.instructions()
        assert e.native(late["job"]["id"]).receipt == "undelivered", "instructions alone are not a receipt"
        e.events(conv, vs3, ("assistant", "your late task finished"))
        assert e.native(late["job"]["id"]).receipt == "undelivered", "a greeting is not item-specific output"
        e.events(conv, vs3, ("assistant", f"Task {late['job']['number']} finished: late result."))
        assert e.native(late["job"]["id"]).delivered_via == "instructions"
        return {"acks": 3, "delivery_notes": 1, "redelivered_next_call": False, "late_result_via": "instructions"}
    finally:
        e.close()


def stored_report(e, job_id):
    from hermes_talk import receipts
    return receipts.load(e.history.get_meta(receipts.report_key(job_id)))


def settled_report(e, job_id, timeout=20):
    report = e.talk.desk.wait_settled(job_id, timeout)
    assert report is not None and report.state in ("sent", "failed", "uncertain"), report
    return report


def telegram_default_receipt(model):
    TELEGRAM.reset()
    e = Env(model)
    try:
        conv, vs = e.call()
        voice_only = "VOICE-ONLY-" + secrets.token_hex(6)
        e.events(conv, vs, ("user", f"private aside {voice_only}"), ("assistant", f"noted {voice_only}"))
        claims = []
        TELEGRAM.on_call = lambda call: claims.append({k: v for k, v in vars(stored_report(e, call_job[0])).items()
                                                       if k in ("state", "text", "destination")})
        call_job = [None]
        _, body = e.task(conv, vs, "receipt alpha", "del_tg1")
        call_job[0] = body["job"]["id"]
        job = e.wait(call_job[0], conv, vs)
        report = settled_report(e, job["id"])
        sent = TELEGRAM.sent()
        assert report.state == "sent" and len(sent) == 1 and report.message_id == sent[0]["message_id"], (report, sent)
        assert claims and claims[0]["state"] == "sending" and claims[0]["text"] == sent[0]["text"], \
            "the report and its destination were stored before the send"
        assert sent[0]["chat_id"] == str(fx.USER_ID) and sent[0]["token"] == fx.BOT_TOKEN and sent[0]["thread_id"] is None
        text = sent[0]["text"]
        assert text.startswith("✅ Hermes task 1 finished: receipt alpha") and "SCRIPTED-ANSWER: receipt alpha" in text, text
        assert len(text) <= 700 and voice_only not in text and "TASK:" not in text and "nobody watches" not in text, text
        listed = e.jobs(conv, vs)[0][job["id"]]
        assert listed["receipt"] == "undelivered" and listed["report"] == {"state": "sent", "destination": "this Telegram chat"}
        _, cancel_body = e.task(conv, vs, "slow 90", "del_tg2")
        e.wait(cancel_body["job"]["id"], conv, vs, states=("running",))
        e.post("task/cancel", {"conversation_id": conv, "job_id": cancel_body["job"]["id"]})
        e.wait(cancel_body["job"]["id"], conv, vs, timeout=40)
        cancelled = settled_report(e, cancel_body["job"]["id"])
        assert cancelled.state == "sent" and TELEGRAM.sent()[-1]["text"].startswith("⏹ Hermes task 2 was cancelled: slow 90")
        secret = "sk-" + "s" * 24
        _, secret_body = e.task(conv, vs, f"secret check {secret}", "del_tg3")
        e.wait(secret_body["job"]["id"], conv, vs)
        settled_report(e, secret_body["job"]["id"])
        assert secret not in TELEGRAM.sent()[-1]["text"] and "[redacted]" in TELEGRAM.sent()[-1]["text"]
        _, big_body = e.task(conv, vs, "big 5000", "del_tg4")
        e.wait(big_body["job"]["id"], conv, vs)
        big = settled_report(e, big_body["job"]["id"])
        last = TELEGRAM.sent()[-1]
        attachment = Path(last["media"][0])
        assert big.state == "sent" and len(last["text"]) <= 700 and "Full result attached" in last["text"], last
        assert last["force_document"] and attachment.is_file() and attachment.is_relative_to(e.home / "plugin-data")
        content = attachment.read_text()
        assert "BIG-START" in content and "BIG-END" in content and len(content) > 5000
        from hermes_talk import receipts
        other_bot = receipts.TelegramSender("654321:" + "x" * 30).send(
            receipts.Destination(**e.native(job["id"]).report_to), "t", None)
        assert receipts.classify(other_bot)[0] == "failed" and len(TELEGRAM.calls) == 4, other_bot
        return {"chat_id": sent[0]["chat_id"], "bot": "the Talk bot that signed the call's initData",
                "other_bot_refused": other_bot["error"],
                "message_id": report.message_id, "text": text, "stored_before_send": claims[0]["state"],
                "voice_receipt": listed["receipt"], "cancelled_report": TELEGRAM.sent()[1]["text"],
                "secret_redacted": True, "large_cover": last["text"],
                "attachment": str(attachment.relative_to(e.home)), "attachment_chars": len(content),
                "transport": "synthetic (tests/agent/synthetic_telegram.py) behind Hermes _send_to_platform"}
    finally:
        TELEGRAM.on_call = None
        e.close()


def telegram_alternate_destination(model):
    TELEGRAM.reset()
    receipts = {"destinations": {"ops": {"target": "telegram:-1009876543210:17", "user_ids": [fx.USER_ID]}}}
    e = Env(model, receipts=receipts)
    try:
        conv, vs = e.call()
        assert "REPORT TO: <name>" in e.instructions() and "one of: ops" in e.instructions()
        _, body = e.task(conv, vs, "alternate alpha\nREPORT TO: ops", "del_alt1")
        job = e.wait(body["job"]["id"], conv, vs)
        assert "REPORT TO" not in e.native(job["id"]).request and "SCRIPTED-ANSWER: alternate alpha" in job["result"]
        report = settled_report(e, job["id"])
        sent = TELEGRAM.sent()
        assert report.state == "sent" and len(sent) == 1, sent
        assert (sent[0]["chat_id"], sent[0]["thread_id"]) == ("-1009876543210", "17"), sent
        assert not [c for c in TELEGRAM.calls if c["chat_id"] == str(fx.USER_ID)], "no duplicate to the private chat"
        _, here = e.task(conv, vs, "alternate beta\nREPORT TO: this chat", "del_alt2")
        e.wait(here["job"]["id"], conv, vs)
        settled_report(e, here["job"]["id"])
        assert TELEGRAM.sent()[-1]["chat_id"] == str(fx.USER_ID)
        jobs_before = len(e.talk.board.jobs)
        conv_b, vs_b = e.call(fx.OTHER_USER_ID)
        refused = {
            "other_user_names_ops": e.task(conv_b, vs_b, "x\nREPORT TO: ops", "del_alt3", fx.OTHER_USER_ID),
            "raw_chat_id": e.task(conv, vs, "x\nREPORT TO: -1001234567890", "del_alt4"),
            "home_channel": e.task(conv, vs, "x\nREPORT TO: home", "del_alt5"),
        }
        statuses = {k: v[0] for k, v in refused.items()}
        assert set(statuses.values()) == {403} and len(e.talk.board.jobs) == jobs_before, refused
        assert "Nothing was started" in refused["raw_chat_id"][1]["error"]
        extra = e.post("task", {"conversation_id": conv, "voice_session_id": vs, "delegation_item_id": "del_alt6",
                                "request": "x", "report_to": "-1001234567890"})[0]
        assert extra == 400
        bad = {}
        from hermes_talk.config import ConfigError
        for name, spec in (("bare", {"target": "telegram", "user_ids": [fx.USER_ID]}),
                           ("stranger", {"target": "telegram:-100555", "user_ids": [4242]})):
            try:
                parse_config(talk_data(e.work, receipts={"destinations": {name: spec}}), e.home)
                bad[name] = "accepted"
            except ConfigError as exc:
                bad[name] = str(exc)[:120]
        assert all(v != "accepted" for v in bad.values()), bad
        return {"named": {"chat_id": sent[0]["chat_id"], "thread_id": sent[0]["thread_id"]},
                "private_duplicate": False, "explicit_private": True, "refused": statuses, "extra_field": extra,
                "config_refusals": bad, "total_sends": len(TELEGRAM.sent())}
    finally:
        e.close()


def telegram_closed_call_delivery(model):
    TELEGRAM.reset()
    e = Env(model)
    try:
        conv, vs = e.call()
        _, body = e.task(conv, vs, "slow 3", "del_closed")
        assert e.post("end", {"conversation_id": conv, "voice_session_id": vs})[0] == 200
        state = json.loads(e.history.get_meta(f"hermes_talk:conversation:{conv}"))
        assert state["voice_sessions"][vs]["ended"] is not None
        deadline = time.time() + 60
        while e.native(body["job"]["id"]).state != "succeeded" and time.time() < deadline:
            time.sleep(0.3)
        report = settled_report(e, body["job"]["id"])
        job = e.native(body["job"]["id"])
        assert report.state == "sent" and TELEGRAM.sent()[0]["chat_id"] == str(fx.USER_ID)
        assert "SCRIPTED-SLOW-DONE" in TELEGRAM.sent()[0]["text"]
        assert job.receipt == "undelivered", "the chat report is not a voice receipt"
        return {"call_ended_before_result": True, "report": report.state, "voice_receipt": job.receipt,
                "message_id": report.message_id}
    finally:
        e.close()


def telegram_ambiguous_send(model):
    TELEGRAM.reset("timeout")
    e = Env(model)
    root = e.root
    try:
        conv, vs = e.call()
        _, a = e.task(conv, vs, "ambiguous alpha", "del_amb1")
        e.wait(a["job"]["id"], conv, vs)
        uncertain = settled_report(e, a["job"]["id"])
        time.sleep(1.5)
        assert uncertain.state == "uncertain" and len(TELEGRAM.calls) == 1, (uncertain, TELEGRAM.calls)
        e.talk.desk.sender.timeout = 1.0
        TELEGRAM.reset("hang")
        _, b = e.task(conv, vs, "ambiguous beta", "del_amb2")
        e.wait(b["job"]["id"], conv, vs)
        hung = settled_report(e, b["job"]["id"], timeout=15)
        assert hung.state == "uncertain" and "may still have been delivered" in hung.error, hung
        TELEGRAM.reset("chat_not_found")
        _, c = e.task(conv, vs, "ambiguous gamma", "del_amb3")
        e.wait(c["job"]["id"], conv, vs)
        failed = settled_report(e, c["job"]["id"])
        time.sleep(1)
        assert failed.state == "failed" and len(TELEGRAM.calls) == 1, failed
        e.talk.desk.backoff = (0.3, 0.3)
        TELEGRAM.reset("refused", "ok")
        _, d = e.task(conv, vs, "ambiguous delta", "del_amb4")
        e.wait(d["job"]["id"], conv, vs)
        retried = settled_report(e, d["job"]["id"])
        assert retried.state == "sent" and retried.attempts == 2 and len(TELEGRAM.calls) == 2, retried
        assert TELEGRAM.calls[0]["text"] == TELEGRAM.calls[1]["text"], "the retry resends the stored report"
        assert len(requests_with(model, "TASK: ambiguous delta")) == 1, "the task was not rerun"
        TELEGRAM.reset("refused", "refused", "refused")
        _, f = e.task(conv, vs, "ambiguous epsilon", "del_amb5")
        e.wait(f["job"]["id"], conv, vs)
        exhausted = settled_report(e, f["job"]["id"])
        assert exhausted.state == "failed" and exhausted.attempts == 3 and "gave up" in exhausted.error, exhausted
        e.close(keep=True)
        TELEGRAM.reset()
        e = Env(model, root=root)
        time.sleep(1)
        after = stored_report(e, a["job"]["id"])
        assert after.state == "uncertain" and not TELEGRAM.calls, "a restart never resends an uncertain report"
        from hermes_talk import receipts
        partial = receipts.classify({"error": "No deliverable text or media remained after processing MEDIA tags",
                                     "warnings": ["Failed to send media /x/report.md: Timed out"]})
        assert partial[0] == "uncertain", partial
        return {"timeout": uncertain.state, "hang": hung.error, "chat_not_found": failed.state,
                "attachment_timeout": partial[0],
                "refused_then_ok": {"state": retried.state, "attempts": retried.attempts},
                "exhausted": exhausted.error, "after_restart_calls": 0,
                "policy": "only refused/rate-limited sends retry, at most receipts.attempts times"}
    finally:
        e.close()


def telegram_restart_dedup(model):
    TELEGRAM.reset()
    e = Env(model)
    root = e.root
    from dataclasses import replace, asdict
    from hermes_talk import jobs, receipts
    try:
        conv, vs = e.call()
        _, body = e.task(conv, vs, "dedup alpha", "del_dd1")
        job_id = body["job"]["id"]
        e.wait(job_id, conv, vs)
        settled_report(e, job_id)
        job = e.native(job_id)
        e.talk.board._settle(job_id)
        e.talk.desk.prepare(job, "regenerated text")
        e.talk.desk.send(job_id)
        e.talk.board.reconcile()
        time.sleep(0.5)
        assert len(TELEGRAM.calls) == 1 and stored_report(e, job_id).text == TELEGRAM.calls[0]["text"]
        crashed = replace(job, id="jb_" + secrets.token_urlsafe(18), number=91, delegation_id="del_dd91",
                          finished=time.time())
        pending = replace(crashed, id="jb_" + secrets.token_urlsafe(18), number=92, delegation_id="del_dd92")
        unreported = replace(crashed, id="jb_" + secrets.token_urlsafe(18), number=93, delegation_id="del_dd93")
        old = replace(crashed, id="jb_" + secrets.token_urlsafe(18), number=94, delegation_id="del_dd94", finished=1.0)
        for j in (crashed, pending, unreported, old):
            e.history.set_meta(jobs.job_key(j.id), jobs.dump(j))
        dest = asdict(receipts.Destination.private(fx.USER_ID))
        for j, state in ((crashed, "sending"), (pending, "pending")):
            r = receipts.Report(job_id=j.id, scope=j.scope, destination=dest, text=f"stored report {j.number}",
                                state=state, attempts=1 if state == "sending" else 0,
                                owner="1:0:dead" if state == "sending" else None, created=time.time())
            e.history.set_meta(receipts.report_key(j.id), json.dumps(asdict(r)))
        e.close(keep=True)
        TELEGRAM.reset()
        e = Env(model, root=root)
        for j in (pending, unreported):
            settled_report(e, j.id)
        time.sleep(1)
        texts = [c["text"] for c in TELEGRAM.calls]
        assert stored_report(e, crashed.id).state == "uncertain", "a claim left by a dead process is not resent"
        assert stored_report(e, job_id).state == "sent" and stored_report(e, old.id) is None
        assert len(texts) == 2 and "stored report 92" in texts and any("Hermes task 93" in x for x in texts), texts
        e.close(keep=True)
        TELEGRAM.reset()
        e = Env(model, root=root)
        time.sleep(1)
        assert not TELEGRAM.calls, "a second restart sends nothing"
        since = e.talk.desk.since
        e.close(keep=True)
        e = Env(model, root=root, receipts={"enabled": False})
        assert e.talk.desk is None and e.history.get_meta(receipts.SINCE_KEY) == ""
        e.close(keep=True)
        e = Env(model, root=root)
        assert e.talk.desk.since > since and not TELEGRAM.calls, "re-enabling reports does not report the gap"
        return {"duplicate_completion_sends": 1, "crash_mid_send": "uncertain, not resent",
                "pending_after_restart": "sent once", "terminal_without_report": "reported once",
                "finished_before_reports_existed": "no report", "second_restart_sends": 0}
    finally:
        e.close()


class FlakyAgent:
    """The real NativeAgent whose first `failures` starts raise OSError before any process exists."""

    def __init__(self, inner, failures):
        self.inner, self.failures, self.starts = inner, failures, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def start(self, *args):
        self.starts += 1
        if self.starts <= self.failures:
            raise OSError("synthetic spawn failure")
        return self.inner.start(*args)


def queue_start_failure(model):
    TELEGRAM.reset()
    from hermes_talk import jobs
    from hermes_talk.agent import NativeAgent
    root = Path(tempfile.mkdtemp(prefix="talk-start-failure-"))
    e = Env(model, root=root)
    conv, vs = e.call()
    scope = e.talk.account(fx.USER_ID).key
    e.close(keep=True)
    try:
        seeded = []
        from hermes_talk.history import NativeHistory
        history = NativeHistory(root / "home")
        for n in (1, 2):
            job = jobs.new_job(scope, conv, vs, 1, f"del_q{n}", n, f"queued {n}", time.time() + n * 0.01,
                               {"kind": "private", "chat_id": fx.USER_ID, "thread_id": None, "name": "",
                                "bot_id": fx.BOT_TOKEN.split(":")[0]})
            history.set_meta(jobs.job_key(job.id), jobs.dump(job))
            seeded.append(job.id)
        history.close()
        config = parse_config(talk_data(root / "work"), root / "home")
        agent = FlakyAgent(NativeAgent(config.tasks, root / "home"), failures=1)
        e = Env(model, root=root, agent=agent)
        first = e.native(seeded[0])
        assert first.state == "failed" and "could not start (OSError)" in first.error, first
        second = e.wait(seeded[1], conv, vs)
        assert agent.starts == 2 and second["state"] == "succeeded" and "SCRIPTED-ANSWER: queued 2" in second["result"]
        failed_report = settled_report(e, seeded[0])
        assert failed_report.state == "sent" and "⚠️ Hermes task 1 failed: queued 1" in failed_report.text
        assert "could not start" in failed_report.text
        always = FlakyAgent(NativeAgent(config.tasks, root / "home"), failures=99)
        e.talk.board.agent = always
        ids = [e.task(conv, vs, f"doomed {n}", f"del_q{n + 2}")[1]["job"]["id"] for n in (1, 2)]
        states = [e.native(i).state for i in ids]
        assert states == ["failed", "failed"] and always.starts == 2, states
        return {"restart_queue": {"first": first.state, "second": second["state"], "spawn_attempts": agent.starts},
                "failed_report": failed_report.text, "always_failing": states}
    finally:
        e.close()


def large_old_unread_result(model):
    TELEGRAM.reset()
    from hermes_talk import context
    text, ids, _ = context.update(1, [("job", "- Task finished. Result: " + "x" * 4000,
                                       "- Hermes task 1 finished. Its result is long; delegate \"result of task 1\".")],
                                  [], 0, "", 3200)
    assert ids == ["job"] and "result of task 1" in text, text
    e = Env(model, result_chars=4000, max_open_per_user=5)
    try:
        conv, vs = e.call()
        _, body = e.task(conv, vs, "big 3900", "del_big")
        job_id = body["job"]["id"]
        assert e.post("end", {"conversation_id": conv, "voice_session_id": vs})[0] == 200
        _, vs2 = e.call()
        job = e.wait(job_id, conv, vs2)
        assert len(job["result"]) > 3200 and "BIG-END" in job["result"]
        status, update = e.post("context", {"conversation_id": conv, "voice_session_id": vs2})
        assert status == 200 and update["sources"]["jobs"] == 1, update
        assert f'"result of task {job["number"]}"' in update["text"] and len(update["text"]) <= 3200, update
        e.post("context/ack", {"conversation_id": conv, "voice_session_id": vs2, "revision": update["revision"]})
        assert e.native(job_id).receipt == "delivered"
        status, full = e.task(conv, vs2, f"result of task {job['number']}", "del_full")
        assert status == 200 and full["channel"] == "speakable" and "BIG-END" in full["reply"] and not full["created"]
        from dataclasses import replace
        from hermes_talk import jobs
        template = e.native(job_id)
        old = []
        for n in range(1, 13):
            j = replace(template, id="jb_" + secrets.token_urlsafe(18), number=100 + n, delegation_id=f"del_old{n}",
                        voice_session_id=vs, result=f"OLD-RESULT-{n}", receipt="undelivered", delivered_via=None)
            e.history.set_meta(jobs.job_key(j.id), jobs.dump(j))
            old.append(j)
        e.close(keep=True)
        e = Env(model, root=e.root, result_chars=4000, max_open_per_user=5)
        _, vs3 = e.call()
        listed = sorted(j["number"] for j in e.jobs(conv, vs3)[0].values())
        assert 101 in listed and 112 in listed, listed
        assert "OLD-RESULT-1 " in e.instructions() or "OLD-RESULT-1\n" in e.instructions() or \
            e.instructions().count("OLD-RESULT-1") >= 1, "the oldest unread result is offered first"
        return {"oversized_update_ids": ids, "synopsis_chars": len(update["text"]),
                "retrieval": "result of task N", "full_reply_chars": len(full["reply"]),
                "old_unread_listed": listed[:3], "oldest_in_instructions": True}
    finally:
        e.close()


NODE_IMAGE = os.environ.get("TALK_NODE_IMAGE", "node:22.22.3-bookworm-slim")
DELIVERY = {}


def delivery_faults():
    """tests/agent/delivery.mjs against the real app.js, once per gate run (node from PATH, else a capped container)."""
    if not DELIVERY:
        script = ["tests/agent/delivery.mjs", "static/talk/app.js"]
        if shutil.which("node"):
            cmd = ["node", *script]
        else:
            cmd = ["docker", "run", "--rm", "--init", "--network", "none", "--memory", "256m", "--memory-swap", "256m",
                   "--cpus", "1", "--pids-limit", "128", "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges:true", "--mount", f"type=bind,src={ROOT},dst=/src,readonly",
                   "--workdir", "/src", "--entrypoint", "node", NODE_IMAGE, *script]
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
        line = next((l for l in out.stdout.splitlines() if l.startswith("DELIVERY_RESULTS ")), "DELIVERY_RESULTS {}")
        DELIVERY.update(exit=out.returncode, results=json.loads(line.split(" ", 1)[1]), log=out.stdout[-3000:] + out.stderr[-1000:])
    return DELIVERY


def javascript_group(name):
    run = delivery_faults()
    group = run["results"].get(name) or []
    assert group and all(r["pass"] for r in group), run["log"]
    return group


def ambiguous_task_submission(model):
    js = javascript_group("ambiguous_task_submission")
    e = Env(model)
    try:
        conv, vs = e.call()
        unique = "lost response " + secrets.token_hex(4)
        body = json.dumps({"initData": init(), "conversation_id": conv, "voice_session_id": vs,
                           "delegation_item_id": "del_lost", "request": unique}).encode()
        with socket.create_connection(("127.0.0.1", e.port), timeout=10) as raw:
            raw.sendall(b"POST /tg-voice/api/task HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                        + f"Origin: {fx.ORIGIN}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        deadline = time.time() + 10
        while not e.talk.board.jobs and time.time() < deadline:
            time.sleep(0.05)
        accepted = list(e.talk.board.jobs.values())
        assert len(accepted) == 1 and accepted[0].delegation_id == "del_lost", "the dropped response's task was accepted"
        from hermes_talk.agent import Readiness
        ready = e.talk.agent.readiness
        e.talk.agent.readiness = lambda: Readiness(False, "synthetic: readiness lapsed")
        status, again = e.task(conv, vs, unique, "del_lost")
        e.talk.agent.readiness = ready
        assert status == 200 and not again["created"] and again["job"]["id"] == accepted[0].id, \
            f"a resubmission finds its job even when readiness lapsed: {again}"
        e.wait(accepted[0].id, conv, vs)
        assert len(requests_with(model, "TASK: " + unique)) == 1, "resubmitting the same delegation ran nothing twice"
        return {"server": {"response_dropped_after_accept": True, "resubmitted_created": again["created"],
                           "native_runs": 1}, "javascript": js}
    finally:
        e.close()


def partial_append_recovery(model):
    return {"javascript": javascript_group("partial_append_recovery"),
            "harness": "tests/agent/delivery.mjs runs the real static/talk/app.js in a node VM with a fault-injected data channel"}


def bounded_native_spool(model):
    from hermes_talk.agent import NativeAgent
    fixture = ROOT / "tests" / "agent" / "noisy_native.py"
    results = {}
    for mode in ("flood", "oversized", "wide"):
        e = Env(model)
        try:
            agent = NativeAgent(e.config.tasks, e.home, command=[sys.executable, str(fixture)],
                                env={**os.environ, "NOISY_MODE": mode})
            e.talk.board.agent = agent
            conv, vs = e.call()
            _, body = e.task(conv, vs, f"noisy {mode}", f"del_noisy_{mode}")
            job_id = body["job"]["id"]
            peak = 0
            deadline = time.time() + 120
            while e.native(job_id).state in ("accepted", "running") and time.time() < deadline:
                output = e.native(job_id).output
                if output and Path(output).exists():
                    peak = max(peak, Path(output).stat().st_size)
                time.sleep(0.05)
            job = e.native(job_id)
            artifact = e.work / "noisy-artifact.bin"
            assert job.state == "succeeded" and "NOISY-RESULT-START" in job.result, job
            assert peak < 2 * 1024 * 1024, f"spool peaked at {peak} bytes"
            assert artifact.stat().st_size == 6 * 1024 * 1024, "the run's own artifact write is not limited"
            gone = time.time() + 10
            while Path(job.output).exists() and time.time() < gone:
                time.sleep(0.05)
            assert not Path(job.output).exists(), "the spool is removed once the job is stored"
            report = settled_report(e, job_id)
            attached = Path(TELEGRAM.calls[-1]["media"][0]).read_text() if TELEGRAM.calls[-1]["media"] else ""
            if mode == "wide":
                assert "ж" * 1000 in attached and "more characters of this result were not kept" in attached, \
                    "a 1.4 MB non-ASCII result is cut at 1 MiB and marked, not lost"
            else:
                assert "NOISY-RESULT-END" in attached, "the large final result is kept whole for the report"
            results[mode] = {"spool_peak_bytes": peak, "stdout_bytes": "about 24 MiB" + (" plus a 9 MiB line" if mode == "oversized" else ""),
                             "artifact_bytes": artifact.stat().st_size, "result_kept": True, "report": report.state}
        finally:
            e.close()
    return results


CHECKS = (native_tool_roundtrip, background_result_delivery, context_update_injection, persistent_two_call_history,
          pulse_visibility_and_cursor, pulse_late_after_fast_start, cross_user_denial, cross_profile_denial, restart_job_reconciliation,
          voice_disconnect_vs_cancel, delegation_id_dedup, plugin_native_install, unauthenticated_job_denial,
          limits_and_approvals, smart_approval_profile, receipt_ack_no_double_speech, telegram_default_receipt,
          telegram_alternate_destination, telegram_closed_call_delivery, telegram_ambiguous_send,
          telegram_restart_dedup, queue_start_failure, large_old_unread_result, ambiguous_task_submission,
          partial_append_recovery, bounded_native_spool)


def gateway_env(model, **kw):
    e = Env(model, **kw)
    assert e.talk.agent.transport == "gateway", "the pinned core's public gateway client selects the RPC transport"
    return e


def rpc_tool_resume_idempotency(model):
    e = gateway_env(model)
    try:
        conv, vs = e.call()
        marker = "FIXTURE-" + secrets.token_hex(8)
        fixture = e.work / "fixture.txt"
        fixture.write_text(marker + "\n")
        status, body = e.task(conv, vs, f"read {fixture}", "del_rpc_tool")
        assert status == 200 and body["created"], body
        job = e.wait(body["job"]["id"], conv, vs)
        assert job["state"] == "succeeded" and marker in job["result"], job
        first = e.native(job["id"])
        assert first.native_session_id and first.admission_id and first.pid is None, first
        snapshot = e.talk.agent.call("session.resume", session_id=first.native_session_id)
        tool_rows = [m for m in snapshot["messages"] if m.get("role") == "tool" and marker in str(m.get("content"))]
        assert tool_rows and not snapshot["running"], "the native session holds the real read_file result"
        stored = e.history.db.get_session(snapshot["stored_session_id"])
        assert first.submission_text is None, "the submitted text is dropped once the admission is stored"
        prompt = e.talk._task_prompt(e.native(job["id"]))[0]
        again = e.talk.agent.call("prompt.submit", session_id=first.native_session_id, text=prompt,
                                  submission_id=job["id"], finite=True)
        assert again["admission_id"] == first.admission_id, "the job id is the gateway's idempotency key"
        from hermes_cli.gateway_client import GatewayRPCError
        try:
            e.talk.agent.call("prompt.submit", session_id=first.native_session_id, text="other text",
                              submission_id=job["id"], finite=True)
            raise AssertionError("a reused submission id with other text must be refused")
        except GatewayRPCError as exc:
            conflict = str(exc)
        assert "admission_conflict" in conflict, conflict
        status, body = e.task(conv, vs, "recall", "del_rpc_recall")
        second = e.wait(body["job"]["id"], conv, vs)
        resumed = e.native(second["id"])
        assert second["state"] == "succeeded" and f"TASK: read {fixture}" in second["result"], second
        assert resumed.native_session_id == first.native_session_id, "the conversation's tasks resume one session"
        from scripted_model import _last_user
        newest = [m for m in model.requests if f"TASK: read {fixture}" in _last_user(m)]
        assert len(newest) == 2, "the first turn ran once (tool call, then its quote); the recall did not rerun it"
        return {"marker": marker, "job": job["id"], "admission": first.admission_id,
                "native_session": first.native_session_id, "stored_source": (stored or {}).get("source"),
                "tool_rows": len(tool_rows), "resubmission_admission": again["admission_id"], "conflict": conflict,
                "resumed_session": resumed.native_session_id, "launch_request": snapshot["info"].get("launch_request")}
    finally:
        e.close()


def rpc_destructive_denial(model):
    seen = {}
    for mode in ("manual", "smart"):
        e = gateway_env(model, profile_extra=f"approvals:\n  mode: {mode}\n")
        try:
            assert e.talk.agent.readiness().ready, e.talk.agent.readiness()
            conv, vs = e.call()
            victim = e.work / "precious"
            victim.mkdir()
            (victim / "keep.txt").write_text("synthetic\n")
            _, body = e.task(conv, vs, f"shell rm -rf {victim}", "del_rpc_rm_" + mode)
            job = e.wait(body["job"]["id"], conv, vs)
            assert (victim / "keep.txt").exists(), f"{mode}: the unattended task deleted nothing"
            assert job["state"] == "succeeded" and "BLOCKED" in job["result"], job
            seen[mode] = job["result"][:300]
        finally:
            e.close()
    for key, value in (("unattended_mode", "approve"), ("single_query_mode", "approve")):
        e = gateway_env(model, profile_extra=f"approvals:\n  mode: manual\n  {key}: {value}\n")
        try:
            assert not e.talk.agent.readiness().ready, key
        finally:
            e.close()
    return {"blocked": seen, "approving_profiles_refused": ["unattended_mode", "single_query_mode"]}


def rpc_cancel_no_late_effect(model):
    e = gateway_env(model)
    try:
        conv, vs = e.call()
        effect = e.work / "late-effect"
        _, body = e.task(conv, vs, f"slowshell 6 touch {effect}", "del_rpc_cancel")
        job_id = body["job"]["id"]
        deadline = time.time() + 60
        while not requests_with(model, f"TASK: slowshell 6 touch {effect}") and time.time() < deadline:
            time.sleep(0.1)
        assert e.native(job_id).admission_id, e.native(job_id)
        status, cancelled = e.post("task/cancel", {"conversation_id": conv, "job_id": job_id})
        assert status == 200 and cancelled["job"]["state"] == "running", "cancel is requested, not yet settled"
        job = e.wait(job_id, conv, vs, timeout=60)
        assert job["state"] == "cancelled", job
        native = e.native(job_id)
        settled = e.talk.agent.call("session.resume", session_id=native.native_session_id, omit_messages=True)
        receipt = e.talk.agent.call("prompt.receipt", session_id=native.native_session_id,
                                    admission_id=native.admission_id)
        assert not settled["running"] and receipt["status"] == "terminal", (settled["running"], receipt)
        time.sleep(9)
        assert not effect.exists(), "no tool ran after the stop"
        return {"job": job_id, "receipt_outcome": receipt["outcome"], "running_after": settled["running"],
                "late_effect": effect.exists()}
    finally:
        e.close()


def rpc_budget_timeout(model):
    """The native run_budget only warns the agent; Talk's whole-job backstop (budget + 60 s) stops the run natively."""
    e = gateway_env(model, run_budget_seconds=20)
    try:
        conv, vs = e.call()
        effect = e.work / "budget-effect"
        started = time.time()
        _, body = e.task(conv, vs, f"slowshell 95 touch {effect}", "del_rpc_budget")
        job = e.wait(body["job"]["id"], conv, vs, timeout=140)
        elapsed = time.time() - started
        assert job["state"] == "failed" and "time budget" in job["error"], job
        assert elapsed >= 75, f"stopped by Talk's backstop, not early: {elapsed:.1f}s"
        native = e.native(job["id"])
        settled = e.talk.agent.call("session.resume", session_id=native.native_session_id, omit_messages=True)
        receipt = e.talk.agent.call("prompt.receipt", session_id=native.native_session_id,
                                    admission_id=native.admission_id)
        assert not settled["running"] and receipt["status"] == "terminal", receipt
        time.sleep(max(0, 100 - (time.time() - started)))
        assert not effect.exists(), "the stopped run started no tool after the backstop"
        return {"job": job["id"], "state": job["state"], "error": job["error"], "seconds": round(elapsed, 1),
                "receipt_outcome": receipt["outcome"], "talk_backstop_s": 80, "late_effect": effect.exists()}
    finally:
        e.close()


def rpc_restart_adoption(model):
    root = Path(tempfile.mkdtemp(prefix="talk-rpc-restart-"))
    home, work = root / "profiles" / "synthetic", root / "work"
    work.mkdir(parents=True)
    fx.write_home(home)
    (home / "config.yaml").write_text(model_config(model))
    data = talk_data(work, receipts={"enabled": False})
    (root / "talk.yaml").write_text(json.dumps(data))
    conv, vs = prepare_call(home, data)
    env = {**os.environ, "HERMES_HOME": str(home), "TG_VOICE_BOT_TOKEN": fx.BOT_TOKEN, "TELEGRAM_PROXY": BLACKHOLE,
           "TG_VOICE_ALLOWED_USER_IDS": f"{fx.USER_ID},{fx.OTHER_USER_ID}"}
    port = free_port()
    procs = [serve(root, port, env)]
    call = {"initData": init(), "conversation_id": conv, "voice_session_id": vs}
    try:
        status, body = request(port, "task", {**call, "delegation_item_id": "del_rpc_r1", "request": "slow 8"})
        assert status == 200, body
        job_id = body["job"]["id"]
        deadline = time.time() + 60
        while not requests_with(model, "TASK: slow 8") and time.time() < deadline:
            time.sleep(0.2)
        procs[0].send_signal(signal.SIGKILL)
        procs[0].wait(10)
        running = stored_job(home, job_id)
        assert running.state == "running" and running.admission_id and running.pid is None, running
        assert gateway_pids(root), "the gateway keeps the admitted work after Talk dies"
        procs.append(serve(root, port, env))
        deadline = time.time() + 90
        while time.time() < deadline:
            job = {j["id"]: j for j in request(port, "tasks", {**call, "initData": init()})[1]["jobs"]}[job_id]
            if job["state"] in TERMINAL:
                break
            time.sleep(0.3)
        adopted = stored_job(home, job_id)
        assert adopted.state == "succeeded" and "adopted after restart" in adopted.notes, adopted
        assert "SCRIPTED-SLOW-DONE" in adopted.result, adopted
        assert len(requests_with(model, "TASK: slow 8")) == 1, "the adopted run was not started twice"
        return {"job": job_id, "admission": adopted.admission_id, "state": adopted.state, "server_restarts": 1}
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                p.wait(10)
        reap_gateways(root)
        shutil.rmtree(root, ignore_errors=True)


def _fault(agent, inject):
    """Wrap the real client's calls: inject(method, params, real) decides; the pinned gateway itself is untouched."""
    real = agent.call

    def call(method, timeout=30.0, **params):
        return inject(method, params, lambda: real(method, timeout, **params))
    agent.call = call
    return real


def rpc_lost_admission_ack(model):
    """The real gateway admits the prompt, then the acknowledgement is lost: Talk reconciles the same admission."""
    e = gateway_env(model)
    try:
        seen = {}

        def inject(method, params, real):
            if method == "prompt.submit" and "lost" not in seen:
                seen["lost"] = real()  # the pinned gateway really admits it
                e.talk.agent.close()  # the connection drops before the answer arrives
                raise ConnectionResetError("synthetic: acknowledgement lost after admission")
            if method == "prompt.submit":
                seen.setdefault("resubmissions", []).append(params["submission_id"])
            return real()
        _fault(e.talk.agent, inject)
        conv, vs = e.call()
        _, body = e.task(conv, vs, "slow 4", "del_rpc_lost_ack")
        job_id = body["job"]["id"]
        assert body["created"] and body["job"]["state"] in ("accepted", "running"), body
        assert seen["lost"].get("admission_id"), "the real gateway admitted the prompt before the loss"
        job = e.wait(job_id, conv, vs, timeout=90)
        native = e.native(job_id)
        assert job["state"] == "succeeded" and "SCRIPTED-SLOW-DONE" in job["result"], job
        assert native.admission_id == seen["lost"]["admission_id"], "the same admission was reconciled, not a new one"
        assert seen.get("resubmissions") and set(seen["resubmissions"]) == {job_id}, seen
        assert len(requests_with(model, "TASK: slow 4")) == 1, "the admitted run ran once"
        return {"job": job_id, "admission": native.admission_id, "lost_ack_admission": seen["lost"]["admission_id"],
                "resubmissions": len(seen["resubmissions"]), "native_runs": 1, "state": job["state"]}
    finally:
        e.close()


def rpc_genuine_refusal(model):
    """A gateway answer that refuses the submission still fails the task, and nothing ran."""
    e = gateway_env(model)
    try:
        def inject(method, params, real):
            if method == "prompt.submit":
                params["session_id"] = "synthetic-unknown-session"  # the real gateway refuses this
            return real()
        _fault(e.talk.agent, inject)
        conv, vs = e.call()
        _, body = e.task(conv, vs, "slow 1 refused", "del_rpc_refused")
        job = e.wait(body["job"]["id"], conv, vs, timeout=60)
        assert job["state"] == "failed" and "did not take" in job["error"], job
        assert not requests_with(model, "TASK: slow 1 refused"), "nothing ran"
        return {"job": job["id"], "state": job["state"], "error": job["error"]}
    finally:
        e.close()


def rpc_stop_survives_outage(model):
    """A cancel whose stop requests hit a transport outage is retried; the run stops with no late effect."""
    e = gateway_env(model)
    try:
        outage = {"until": 0.0, "failed": []}

        def inject(method, params, real):
            if time.time() < outage["until"]:
                outage["failed"].append(method)
                e.talk.agent.close()
                raise ConnectionResetError("synthetic: gateway unreachable")
            return real()
        _fault(e.talk.agent, inject)
        conv, vs = e.call()
        effect = e.work / "outage-effect"
        _, body = e.task(conv, vs, f"slowshell 14 touch {effect}", "del_rpc_outage")
        job_id = body["job"]["id"]
        deadline = time.time() + 60
        while not requests_with(model, f"TASK: slowshell 14 touch {effect}") and time.time() < deadline:
            time.sleep(0.1)
        assert e.native(job_id).admission_id, e.native(job_id)
        outage["until"] = time.time() + 4
        status, cancelled = e.post("task/cancel", {"conversation_id": conv, "job_id": job_id})
        assert status == 200 and cancelled["job"]["state"] == "running", cancelled
        job = e.wait(job_id, conv, vs, timeout=60)
        assert outage["failed"], "the stop met the outage"
        assert job["state"] == "cancelled", job
        native = e.native(job_id)
        settled = e.talk.agent.call("session.resume", session_id=native.native_session_id, omit_messages=True)
        receipt = e.talk.agent.call("prompt.receipt", session_id=native.native_session_id,
                                    admission_id=native.admission_id)
        assert not settled["running"] and receipt["status"] == "terminal", (settled["running"], receipt)
        time.sleep(16)
        assert not effect.exists(), "no tool ran after the recovered stop"
        return {"job": job_id, "failed_during_outage": sorted(set(outage["failed"])), "receipt_outcome": receipt["outcome"],
                "running_after": settled["running"], "late_effect": effect.exists()}
    finally:
        e.close()


def rpc_composite_toolsets(model):
    """A composite `tasks.toolsets` pick (`hermes-telegram`) through the pinned gateway's real create policy.

    The bare composite is what the gateway refuses (`invalid_params`, field `toolsets`): its default-off members
    are not granted on this surface unless named. Talk's launch pick names them and adds no tool."""
    import toolsets
    from gateway.session_policy import build_policy
    from hermes_cli.config import load_config
    from hermes_state_runtime import RuntimeStoreError
    e = gateway_env(model, toolsets=["hermes-telegram"])
    try:
        conv, vs = e.call()
        marker = "FIXTURE-" + secrets.token_hex(8)
        fixture = e.work / "fixture.txt"
        fixture.write_text(marker + "\n")
        status, body = e.task(conv, vs, f"read {fixture}", "del_rpc_composite")
        assert status == 200 and body["created"], body
        job = e.wait(body["job"]["id"], conv, vs)
        assert job["state"] == "succeeded" and marker in job["result"], job
        stored = e.native(job["id"])
        assert stored.native_session_id and stored.admission_id, stored
        config = load_config()
        bare = {"cwd": str(e.work), "toolsets": ["hermes-telegram"], "max_turns": 8, "run_budget": 120.0}
        try:
            build_policy(bare, config, private_secrets={}, workspace_posture=True)
            raise AssertionError("the real create policy accepted the bare composite: this check no longer discriminates")
        except RuntimeStoreError as exc:
            assert (exc.reason, exc.field) == ("invalid_params", "toolsets"), (exc.reason, exc.field)
        from hermes_talk.gateway_rpc import launch_toolsets
        pick = launch_toolsets(["hermes-telegram"])
        granted = build_policy({**bare, "toolsets": pick}, config, private_secrets={}, workspace_posture=True)
        tools = lambda names: {t for n in names for t in toolsets.resolve_toolset(n)}
        assert tools(pick) == tools(["hermes-telegram"]) == tools(granted.toolsets), "the pick adds or loses tools"
        return {"bare_refusal": {"reason": "invalid_params", "field": "toolsets"}, "pick": pick,
                "granted_toolsets": list(granted.toolsets), "job_state": job["state"]}
    finally:
        e.close()


RPC_CHECKS = (rpc_composite_toolsets, rpc_tool_resume_idempotency, rpc_destructive_denial, rpc_cancel_no_late_effect, rpc_budget_timeout,
              rpc_restart_adoption, rpc_lost_admission_ack, rpc_genuine_refusal, rpc_stop_survives_outage,
              background_result_delivery, persistent_two_call_history, delegation_id_dedup,
              receipt_ack_no_double_speech, telegram_default_receipt, telegram_alternate_destination,
              telegram_ambiguous_send, cross_user_denial, unauthenticated_job_denial)


def run_inner(receipts, only, rpc_target=None):
    global TELEGRAM
    from scripted_model import ScriptedModel
    from synthetic_telegram import SyntheticTelegram
    TELEGRAM = SyntheticTelegram()
    TELEGRAM.install()
    os.environ["TELEGRAM_PROXY"] = BLACKHOLE
    model = ScriptedModel()
    failures = 0
    if rpc_target:
        import hermes_cli
        assert Path(hermes_cli.__file__).resolve().is_relative_to(Path(rpc_target).resolve()), hermes_cli.__file__
    selected = [fn for fn in (RPC_CHECKS if rpc_target else CHECKS) if not only or fn.__name__ in only]
    for fn in selected:
        started = time.time()
        try:
            evidence, code = fn(model), 0
            print(f"PASS [agent] {fn.__name__} ({time.time() - started:.1f}s)", flush=True)
        except Exception as exc:
            evidence, code = {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-2000:]}, 1
            failures += 1
            print(f"FAIL [agent] {fn.__name__}: {type(exc).__name__}: {exc}", flush=True)
        if receipts:
            Path(receipts).mkdir(parents=True, exist_ok=True)
            Path(receipts, f"{fn.__name__}.json").write_text(json.dumps(
                {"capability": fn.__name__, "exit": code, "seconds": round(time.time() - started, 1),
                 "runtime": sys.executable, "model_boundary": "scripted-deterministic-model (labelled; not a real model)",
                 "evidence": evidence}, indent=2, default=str))
    model.close()
    print(f"{len(selected) - failures}/{len(selected)} agent checks passed")
    return 1 if failures else 0


def hermes_python():
    explicit = os.environ.get("HERMES_TALK_HERMES_PYTHON")
    if explicit:
        return explicit
    found = sorted(glob.glob(os.path.expanduser("~/.hermes/installs/*/environments/*/venv/bin/python")))
    return found[0] if found else None


BROWSER_IMAGE = os.environ.get("TALK_BROWSER_IMAGE", "django-admin-repl-verify-browser:pw1.63.0")


def run_browser(out_dir, runtime):
    """The Chromium delegation job in a capped, network-less container with the Hermes runtime mounted read-only."""
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    venv = Path(runtime).parent.parent
    install = venv.parents[2]
    interpreter = Path(os.path.realpath(runtime)).parents[1]
    cmd = ["docker", "run", "--rm", "--init", "--network", "none", "--memory", "2g", "--memory-swap", "2g",
           "--cpus", "2", "--pids-limit", "384", "--shm-size", "256m", "--user", f"{os.getuid()}:{os.getgid()}",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:rw,size=768m",
           "--env", "HOME=/tmp/home", "--env", "TMPDIR=/tmp", "--env", f"HERMES_TALK_HERMES_PYTHON={runtime}",
           "--mount", f"type=bind,src={ROOT},dst=/src,readonly",
           "--mount", f"type=bind,src={install},dst={install},readonly",
           "--mount", f"type=bind,src={interpreter},dst={interpreter},readonly",
           "--mount", f"type=bind,src={ROOT / 'tests/agent/browser.mjs'},dst=/app/agent-job.mjs,readonly",
           "--mount", f"type=bind,src={out},dst=/output", "--workdir", "/src",
           "--entrypoint", "node", BROWSER_IMAGE, "/app/agent-job.mjs", "/output"]
    result = subprocess.run(cmd, timeout=900)
    print("TALK_AGENT_BROWSER_" + ("OK" if result.returncode == 0 else "FAILED"))
    return result.returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-hermes", action="store_true")
    parser.add_argument("--browser", metavar="OUT_DIR")
    parser.add_argument("--receipts", metavar="DIR")
    parser.add_argument("--rpc-target", metavar="SDK")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inner:
        return run_inner(args.receipts, args.only, args.rpc_target)
    runtime = hermes_python()
    if not runtime:
        print("SKIPPED agent checks: no Hermes runtime found; set HERMES_TALK_HERMES_PYTHON")
        return 1 if args.require_hermes else 0
    if args.browser:
        return run_browser(args.browser, runtime)
    scratch = tempfile.mkdtemp(prefix="talk-agent-gate-")
    try:
        cmd = [runtime, "-B", str(ROOT / "verify-agent.py"), "--inner"]
        cmd += ["--receipts", str(Path(args.receipts).resolve())] if args.receipts else []
        for name in args.only:
            cmd += ["--only", name]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HERMES_HOME": scratch}
        if args.rpc_target:
            sdk = Path(args.rpc_target).resolve()
            cmd += ["--rpc-target", str(sdk)]
            # A synthetic HOME and temp dir: the pinned source stays read-only and no user state is touched.
            env.update(PYTHONPATH=str(sdk), HOME=scratch, TMPDIR=scratch)
        result = subprocess.run(cmd, cwd=scratch, env=env, timeout=1500)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    tag = "TALK_AGENT_RPC_" if args.rpc_target else "TALK_AGENT_"
    print(tag + ("OK" if result.returncode == 0 else "FAILED"))
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
