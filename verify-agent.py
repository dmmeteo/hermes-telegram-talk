#!/usr/bin/env python3
"""Hermes Talk agent gate: voice tasks through the real Hermes agent, with synthetic data only.

  python3 verify-agent.py --require-hermes [--receipts DIR]
  python3 verify-agent.py --browser OUT_DIR    Chromium delegation job (real app.js, server and tasks)

Each check runs the real Talk server over HTTP against a disposable HERMES_HOME. Tasks run as real
`hermes chat` processes from the Hermes runtime (HERMES_TALK_HERMES_PYTHON, else the first
~/.hermes/installs/*/environments/*/venv/bin/python): Hermes' own AIAgent, tool registry, tool
execution, approval policy and SessionDB. Only the inference provider is replaced, by the labelled
scripted model in tests/agent/scripted_model.py. Nothing calls a paid or live provider. Without a
runtime the gate prints SKIPPED, and --require-hermes makes that a failure.
"""
import argparse
import glob
import http.client
import json
import os
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
PLUGIN_FILES = ("plugin.yaml", "__init__.py", "server.py", "hermes_talk", "static", "LICENSES")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


def model_config(model, extra=""):
    return (f"model:\n  default: scripted-deterministic-model\n  provider: custom\n  base_url: {model.base_url}\n"
            f"  api_key: synthetic-local-key\n{extra}")


def talk_data(workdir, profile="synthetic", **tasks):
    return fx.config_data(profile={"id": profile, "display_name": "Synthetic Talk"},
                          tasks={"enabled": True, "user_ids": [fx.USER_ID, fx.OTHER_USER_ID],
                                 "toolsets": ["file", "terminal"], "workdir": str(workdir),
                                 "run_budget_seconds": 120, **tasks})


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

    def __init__(self, model, root=None, profile="synthetic", profile_extra="", **tasks):
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
        self.talk = TalkRuntime(self.config, fx.BOT_TOKEN, self.history, home=self.home)
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
                    board.agent.stop(job.pid, job.pid_start)
            board.wait_idle(20)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.history.close()
        if not keep:
            shutil.rmtree(self.root, ignore_errors=True)


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
        conv, vs = e.call()
        assert "Synthetic owner build" in e.instructions()
        state = lambda: json.loads(e.history.get_meta(f"hermes_talk:conversation:{conv}"))
        assert state()["activity_cursor"] is None, "the cursor waits for the assistant to speak"
        e.events(conv, vs, ("assistant", "hello"))
        spoke = state()["activity_cursor"]
        assert spoke and spoke[1] == before, spoke
        later = e.history.create_session("Synthetic later deploy", "cli")
        e.history.append(later, [{"role": "assistant", "content": "deployed", "platform_message_id": "l1",
                                  "timestamp": time.time() + 1}])
        status, update = e.post("context", {"conversation_id": conv, "voice_session_id": vs})
        assert status == 200 and "Synthetic later deploy" in update["text"] and "Synthetic owner build" not in update["text"]
        assert state()["activity_cursor"] == spoke, "an unacked update leaves the cursor in place"
        e.post("context/ack", {"conversation_id": conv, "voice_session_id": vs, "revision": update["revision"]})
        assert state()["activity_cursor"][1] == later
        _, vs2 = e.call()
        assert "Synthetic later deploy" not in e.instructions() and "Synthetic owner build" not in e.instructions()
        return {"briefed_at_open": before, "cursor_after_speech": spoke, "delivered_mid_call": later,
                "cursor_after_ack": state()["activity_cursor"]}
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
    data = talk_data(work)
    (root / "talk.yaml").write_text(json.dumps(data))
    conv, vs = prepare_call(home, data)
    env = {**os.environ, "HERMES_HOME": str(home), "TG_VOICE_BOT_TOKEN": fx.BOT_TOKEN,
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
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(user_home), "PYTHONDONTWRITEBYTECODE": "1"}
    native = lambda *args: subprocess.run([hermes, *args], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    proc = None
    profile = user_home / ".hermes" / "profiles" / "synthetic"
    target = profile / "plugins" / "hermes-talk"
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
        (profile / "config.yaml").write_text(model_config(model, "plugins:\n  enabled:\n    - hermes-talk\n"))
        listed = [line.split() for line in native("-p", "synthetic", "plugins", "list", "--plain", "--no-bundled").stdout.splitlines()]
        assert ["enabled", "user", "0.2.0", "hermes-talk"] in listed, listed
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
        assert not (user_home / ".hermes" / "installs").exists(), "no Hermes runtime was provisioned"
        return {"installed_server": str(target / "server.py").replace(str(root), "<disposable>"), "marker": marker,
                "job": job["id"], "tasks_available": True}
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
        assert e.native(late["job"]["id"]).delivered_via == "instructions"
        return {"acks": 3, "delivery_notes": 1, "redelivered_next_call": False, "late_result_via": "instructions"}
    finally:
        e.close()


CHECKS = (native_tool_roundtrip, background_result_delivery, context_update_injection, persistent_two_call_history,
          pulse_visibility_and_cursor, cross_user_denial, cross_profile_denial, restart_job_reconciliation,
          voice_disconnect_vs_cancel, delegation_id_dedup, plugin_native_install, unauthenticated_job_denial,
          limits_and_approvals, receipt_ack_no_double_speech)


def run_inner(receipts, only):
    from scripted_model import ScriptedModel
    model = ScriptedModel()
    failures = 0
    selected = [fn for fn in CHECKS if not only or fn.__name__ in only]
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
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inner:
        return run_inner(args.receipts, args.only)
    runtime = hermes_python()
    if not runtime:
        print("SKIPPED agent checks: no Hermes runtime found; set HERMES_TALK_HERMES_PYTHON")
        return 1 if args.require_hermes else 0
    if args.browser:
        return run_browser(args.browser, runtime)
    scratch = tempfile.mkdtemp(prefix="talk-agent-gate-")
    try:
        cmd = [runtime, str(ROOT / "verify-agent.py"), "--inner"]
        cmd += ["--receipts", str(Path(args.receipts).resolve())] if args.receipts else []
        for name in args.only:
            cmd += ["--only", name]
        result = subprocess.run(cmd, cwd=scratch, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                                       "HERMES_HOME": scratch}, timeout=1500)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print("TALK_AGENT_OK" if result.returncode == 0 else "TALK_AGENT_FAILED")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
