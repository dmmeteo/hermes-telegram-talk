#!/usr/bin/env python3
"""Work-model gate: micro requests stay responsive beside confirmed T3 work, which Talk follows to its real end.

  python3 tests/work/run.py      prints TALK_WORK_OK

The real Talk server, TalkRuntime, JobBoard, ReportDesk, agent.NativeAgent, spool.py and the post_tool_call hook body
(work.observe, run inside each task process with the HERMES_TALK_JOB Talk gave it) run over HTTP with signed synthetic
initData and the in-memory SessionDB stand-in. Only `hermes chat` is replaced, by tests/work/runner.py, and T3 by the
file-backed tests/work/fake_t3.py behind the same t3_control result shapes. No model, provider, Hermes core, T3 or
Telegram is used; status messages go to a recording sender.
"""
import http.client
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from hermes_talk import fixtures as fx  # noqa: E402
from hermes_talk import jobs, notify, thread_gateway, work  # noqa: E402
from hermes_talk.agent import NativeAgent, Readiness  # noqa: E402
from hermes_talk.config import ConfigError, load_config  # noqa: E402
from hermes_talk.history import MemoryHistory  # noqa: E402
from hermes_talk.talk import TalkRuntime  # noqa: E402
from fake_t3 import FakeT3  # noqa: E402

RUNNER = ROOT / "tests" / "work" / "runner.py"
TASKS = "tasks:\n  enabled: true\n  user_ids: [{user}, {other}]\n  toolsets: [file]\n  workdir: {work}\n"
ROLLBACK_REVISION = "35f8fa12f5f5f39932718b41b30582c54ca5ad59"  # the deployed Talk a rollback returns to


def yaml_text(work_dir, tasks_extra="", receipts=""):
    base = fx.config_data()
    head = "".join(f"{key}: {json.dumps(value)}\n" for key, value in base.items())
    return head + TASKS.format(user=fx.USER_ID, other=fx.OTHER_USER_ID, work=work_dir) + tasks_extra + receipts


class BoundedAgent(NativeAgent):
    def readiness(self):
        return Readiness(True, "")


class Posts:
    def __init__(self):
        self.calls, self.lock = [], threading.Lock()

    def send(self, destination, text):
        with self.lock:
            self.calls.append(("send", str(7000 + len(self.calls)), text, destination.chat_id))
            return {"success": True, "message_id": self.calls[-1][1]}

    def edit(self, destination, message_id, text):
        with self.lock:
            self.calls.append(("edit", str(message_id), text, destination.chat_id))
        return {"success": True, "message_id": str(message_id)}

    def shown(self):
        with self.lock:
            out = {}
            for _, message_id, text, _ in self.calls:
                out.setdefault(message_id, []).append(text)
            return out

    def sends(self):
        with self.lock:
            return [c for c in self.calls if c[0] == "send"]


class World:
    def __init__(self, root=None, max_concurrent=1, history=None, model=None):
        self.root = Path(root or tempfile.mkdtemp(prefix="talk-work-"))
        self.home = self.root / "home"
        if not self.home.exists():
            fx.write_home(self.home)
        self.work = self.root / "work"
        self.work.mkdir(exist_ok=True)
        (self.root / "release").mkdir(exist_ok=True)
        (self.root / "talk.yaml").write_text(yaml_text(self.work, f"  max_concurrent: {max_concurrent}\n"))
        self.config = load_config(self.root / "talk.yaml")
        self.history = history or MemoryHistory()
        self.agent = BoundedAgent(self.config.tasks, self.home, command=[sys.executable, str(RUNNER)],
                                  env={**os.environ, "TALK_WORK_DIR": str(self.root),
                                       "PYTHONPATH": str(ROOT)})
        self.t3 = FakeT3(self.root / "t3")
        self.posts = Posts()
        self.fake = fx.FakeUpstream()
        self.talk = self.httpd = None
        self.model = model  # the host ctx.llm route as TalkRuntime sees it (pulse_model.PulseModel); None: unbound

    def serve(self):
        self.talk = TalkRuntime(self.config, fx.BOT_TOKEN, self.history, home=self.home, agent=self.agent,
                                sender=self.posts, skill=fx.file_skill(self.home), t3=work.T3(self.t3.dispatch),
                                model=self.model)
        self.talk.board.follow_s = 0.2
        app = server.VoiceApp(fx.BOT_TOKEN, {fx.USER_ID, fx.OTHER_USER_ID}, fx.ORIGIN, resolve=self.fake.resolve,
                              post=self.fake.post, talk=self.talk)
        app.limiter.limit = 1000
        for limiter in app.talk_limiters.values():
            limiter.limit = 1000
        self.httpd = server.BoundedServer(("127.0.0.1", 0), server.make_handler(app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop_serving(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None

    def post(self, route, fields, user=fx.USER_ID):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        body = json.dumps({"initData": fx.sign_init_data(user, int(time.time())), **fields})
        conn.request("POST", "/tg-voice/api/" + route, body=body,
                     headers={"Content-Type": "application/json", "Origin": fx.ORIGIN})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def call(self, user=fx.USER_ID):
        status, boot = self.post("bootstrap", {}, user)
        assert status == 200, boot
        status, answer = self.post("session", {"sdp": fx.OFFER, "conversation_id": boot["conversation_id"]}, user)
        assert status == 200, answer
        return boot["conversation_id"], answer["voice_session_id"]

    def task(self, conv, vs, text, delegation=None, user=fx.USER_ID):
        return self.post("task", {"conversation_id": conv, "voice_session_id": vs,
                                  "delegation_item_id": delegation or f"del_{text.split()[-1]}_{time.time_ns()}",
                                  "request": text}, user)

    def job(self, key):
        found = [j for j in self.talk.board.jobs.values() if j.request.split()[-1] == key]
        assert len(found) == 1, (key, [j.request for j in self.talk.board.jobs.values()])
        return self.talk.board.jobs[found[0].id]

    def ledger(self, event=None, key=None):
        path = self.root / "ledger.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if (event is None or r["event"] == event) and (key is None or r["key"] == key)]

    def release(self, key):
        (self.root / "release" / key).touch()

    def until(self, predicate, what, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}: "
                             f"{ {j.request: (j.state, j.kind, j.work_status) for j in self.talk.board.jobs.values()} }")

    def state(self, key):
        return self.job(key).state

    def scope(self, user=fx.USER_ID):
        return self.talk.account(user).key

    def settle_reports(self):
        for job in list(self.talk.board.jobs.values()):
            self.talk.desk.wait_settled(job.id, 10)

    def close(self):
        for path in (self.root / "t3").glob("*.json"):
            self.t3.put(path.stem, outcome="completed")
        for row in self.ledger("start"):
            self.release(row["key"])
        if self.talk is not None and self.talk.board is not None:
            self.talk.board.wait_idle(20)
        if self.httpd is not None:
            self.stop_serving()
        shutil.rmtree(self.root, ignore_errors=True)


def peak(rows, keys):
    level, best = 0, 0
    for row in sorted((r for r in rows if r["key"] in keys and r["event"] in ("start", "end", "stop")),
                      key=lambda r: r["at"]):
        level += 1 if row["event"] == "start" else -1
        best = max(best, level)
    return best


def slow_t3_keeps_requests_responsive():
    """One slot: a T3 assignment whose turn even keeps waiting on T3 never blocks quick requests or controls."""
    w = World(max_concurrent=1).serve()
    try:
        conv, vs = w.call()
        status, body = w.task(conv, vs, "codewait c1\nTITLE: Fix the synthetic parser")
        assert status == 200 and body["created"] and body["job"]["kind"] == "micro", body
        w.until(lambda: w.job("c1").large, "c1 promoted by its own T3 start")
        assert w.job("c1").state == "running" and w.ledger("stop", "c1") == [], "the waiting turn is left alone"
        status, body = w.task(conv, vs, "hold m1")
        assert status == 200 and body["created"], body
        w.until(lambda: w.state("m1") == "running", "a quick request starts while c1's turn still waits on T3")
        assert not w.ledger("end", "c1"), "c1's turn is still running"
        pending = []
        for n in range(2, 12):
            status, body = w.task(conv, vs, f"hold m{n}")
            assert status == 200 and body["created"], (n, status, body)
            pending.append(f"m{n}")
        assert [w.state(k) for k in pending] == ["accepted"] * 10, "ten more than the old cap of 3 wait, none refused"
        status, listed = w.task(conv, vs, "CONTROL: list")
        assert status == 200 and not listed["created"] and listed["channel"] == "speakable", listed
        assert "running as T3 code work" in listed["reply"] and "Task 3 (hold m2) is accepted" in listed["reply"], listed
        status, st = w.task(conv, vs, "CONTROL: status 1")
        assert status == 200 and "running as T3 code work" in st["reply"], st
        status, cancelled = w.task(conv, vs, "CONTROL: cancel 12")
        assert status == 200 and cancelled["job"]["state"] == "cancelled" and w.state("m11") == "cancelled", cancelled
        assert len(w.talk.board.jobs) == 12, "controls create no task"
        w.release("c1")
        w.until(lambda: w.ledger("end", "c1"), "c1's turn ends")
        time.sleep(0.6)
        assert w.state("c1") == "running" and w.job("c1").turn["state"] == "succeeded", \
            "the outer turn finished; its T3 work still runs and the job stays open"
        w.t3.finish("th-c1", "completed", "Parser fixed; tests pass.")
        w.until(lambda: w.state("c1") == "succeeded", "c1 settles from T3")
        assert w.job("c1").result == "Parser fixed; tests pass.", w.job("c1").result
        for key in pending:
            w.release(key)
        w.release("m1")
        w.until(lambda: all(w.state(k) in ("succeeded", "cancelled") for k in ["m1", *pending]), "all quick done")
        micro = {f"m{n}" for n in range(1, 12)}
        assert peak(w.ledger(), micro) == 1, "the execution slot still bounds quick requests"
        assert "m11" not in {r["key"] for r in w.ledger("start")}, "the cancelled queued request never started"
        assert [w.state(f"m{n}") for n in range(1, 11)] == ["succeeded"] * 10
        return {"m1_started_while_c1_turn_waited": True, "accepted_beyond_old_cap": len(pending),
                "peak_quick": 1, "c1_result": w.job("c1").result, "list": listed["reply"][:160]}
    finally:
        w.close()


def promotion_is_exact():
    w = World(max_concurrent=3).serve()
    try:
        conv, vs = w.call()
        w.t3.put("th-old", outcome="completed", count=1, messages=[{"role": "assistant", "text": "old"}])
        for text in ("look th-old r1", "refused r2", "hold r3", "code s1"):
            assert w.task(conv, vs, text)[0] == 200
        w.until(lambda: w.state("r1") == "succeeded" and w.state("r2") == "succeeded", "reads and refusals settle")
        w.until(lambda: w.job("s1").large, "s1 promoted")
        assert not w.job("r1").large and not w.job("r2").large and not w.job("r3").large, "only s1 is large"
        assert [l.thread_id for l in w.job("s1").links] == ["th-s1"]
        assert w.job("r3").state == "running" and not w.job("r3").links, "a concurrent quick request is untouched"
        assert {r["job"] for r in w.ledger("start")} == {j.id for j in w.talk.board.jobs.values()}, \
            "each turn carried its own job id"
        assert w.task(conv, vs, "send th-s1 u1")[0] == 200
        w.until(lambda: w.job("u1").large, "a send to s1's thread promotes the request that sent it")
        link = w.job("u1").links[0]
        assert link.continues == w.job("s1").number and link.queued and link.status == "queued", link
        assert w.state("s1") == "running" and w.job("s1").work_status == "running"
        w.t3.finish("th-s1", "completed", "First run done.")
        w.until(lambda: w.state("s1") == "interrupted" and w.job("u1").work_status == "running",
                "s1's run ends; the queued update run starts at once")
        assert w.job("s1").unknown and w.job("s1").result == "", \
            ("T3 never showed s1's run as latest and settled, so its outcome is unknown", w.job("s1"))
        w.t3.finish("th-s1", "completed", "Update applied.")
        w.until(lambda: w.state("u1") == "succeeded", "u1 settles from its own run")
        assert w.job("u1").result == "Update applied."
        w.release("r3")
        w.until(lambda: w.state("r3") == "succeeded", "r3")
        assert w.job("r3").result == "done r3"
        w.t3.put("th-idle", outcome="completed", count=1, messages=[{"role": "assistant", "text": "old"}])
        assert w.task(conv, vs, "send th-idle a1")[0] == 200
        w.until(lambda: w.job("a1").large and w.job("a1").work_status == "running", "a1 started a run on an idle thread")
        assert w.task(conv, vs, "send th-idle a2")[0] == 200
        w.until(lambda: w.job("a2").large and w.job("a2").work_status == "queued", "a2 queued behind a1's run")
        w.t3.finish("th-idle", "completed", "A1 result.")
        w.until(lambda: w.state("a1") == "interrupted" and w.job("a2").work_status == "running", "a1 ends, a2 runs")
        assert w.job("a1").unknown and w.job("a1").result == "", w.job("a1")
        assert w.task(conv, vs, "CONTROL: cancel " + str(w.job("a2").number))[0] == 200
        w.until(lambda: w.state("a2") == "cancelled", "a2 cancelled from T3")
        assert w.state("a1") == "interrupted" and w.t3.get("th-idle")["interrupts"] == 1, "only a2's own run was stopped"
        return {"large": ["s1", "u1", "a1", "a2"], "micro": ["r1 (read)", "r2 (refused)", "r3"],
                "u1_continues": link.continues, "superseded_unknown": ["s1", "a1"]}
    finally:
        w.close()


def other_user_no_crossover():
    w = World(max_concurrent=3).serve()
    try:
        conv_a, vs_a = w.call()
        conv_b, vs_b = w.call(fx.OTHER_USER_ID)
        assert w.task(conv_a, vs_a, "code a1")[0] == 200
        assert w.task(conv_b, vs_b, "code b1", user=fx.OTHER_USER_ID)[0] == 200
        w.until(lambda: w.job("a1").large and w.job("b1").large, "both promoted")
        assert [l.thread_id for l in w.job("a1").links] == ["th-a1"] and [l.thread_id for l in w.job("b1").links] == ["th-b1"]
        status, body = w.task(conv_b, vs_b, "CONTROL: cancel 1", user=fx.OTHER_USER_ID)
        assert status == 200 and body["job"]["id"] == w.job("b1").id, "task numbers resolve within the caller's own"
        status, foreign = w.post("task/cancel", {"conversation_id": conv_b, "job_id": w.job("a1").id}, fx.OTHER_USER_ID)
        assert status == 404, foreign
        w.until(lambda: w.state("b1") == "cancelled", "b1 cancelled from T3 state")
        assert w.state("a1") == "running" and w.t3.get("th-a1")["interrupts"] == 0
        notify.toggle(w.home, w.scope(fx.OTHER_USER_ID), notify.MICRO, w.talk.notify_defaults())
        assert w.talk.visibility(w.scope()) == notify.Visibility(True, False), "a switch is per account"
        w.t3.finish("th-a1", "completed", "A done.")
        w.until(lambda: w.state("a1") == "succeeded", "a1")
        w.settle_reports()
        chats = {c[3] for c in w.posts.sends()}
        assert len(w.posts.sends()) == 2 and chats == {fx.USER_ID, fx.OTHER_USER_ID}, w.posts.calls
        return {"a1": w.state("a1"), "b1": w.state("b1"), "status_message_chats": sorted(chats)}
    finally:
        w.close()


def restart_and_uncertain_admission():
    w = World(max_concurrent=3).serve()
    try:
        conv, vs = w.call()
        for text in ("lost x1", "lostnone x2", "code x3"):
            assert w.task(conv, vs, text)[0] == 200
        w.until(lambda: w.state("x2") == "succeeded", "a lost start T3 never shows settles from its own turn")
        assert not w.job("x2").large and w.job("x2").links[0].status == work.DROPPED, w.job("x2").links
        w.until(lambda: w.job("x1").large and w.job("x3").large, "the lost start T3 shows is confirmed")
        w.settle_reports()
        sends_before = len(w.posts.sends())
        history, root = w.history, w.root
        w.stop_serving()
        w.talk.board.follow_s = 3600  # the old process stops following, as if it had exited
        old = w.talk
        w.talk = None
        w2 = World(root=root, max_concurrent=3, history=history)
        w2.posts = w.posts
        w2.serve()
        old.board.jobs.clear()
        w = w2
        assert w.state("x1") == "running" and w.state("x3") == "running", "the restart keeps following both"
        w.t3.finish("th-x1", "completed", "X1 done.")
        w.t3.finish("th-x3", "failed", "Build broke.")
        w.until(lambda: w.state("x1") == "succeeded" and w.state("x3") == "failed", "outcomes from T3 after restart")
        assert "Build broke." in w.job("x3").error, w.job("x3").error
        assert len(w.ledger("start")) == 3 and w.t3.get("th-x1")["count"] == 1, "nothing was launched twice"
        w.settle_reports()
        assert len(w.posts.sends()) == sends_before == 2, ("one message per visible task", w.posts.calls)
        finals = sorted(texts[-1] for texts in w.posts.shown().values())
        assert finals == ["✅ lost x1", "❌ code x3"], finals
        return {"x1": "confirmed by T3, followed across restart", "x2": "dropped, settled from its turn",
                "x3": "failed from T3", "status_messages": len(w.posts.sends())}
    finally:
        w.close()


def cancel_large_and_queued_and_blocked():
    w = World(max_concurrent=1).serve()
    try:
        conv, vs = w.call()
        for text in ("code k1", "codewait k2", "hold q1", "hold q2"):
            assert w.task(conv, vs, text)[0] == 200
        w.until(lambda: w.job("k1").large, "k1 promoted")
        w.until(lambda: w.job("k2").large, "k2 promoted")
        w.until(lambda: w.state("q1") == "running", "q1 runs")
        assert w.state("q2") == "accepted"
        assert w.task(conv, vs, "CONTROL: cancel 4")[1]["job"]["state"] == "cancelled"
        w.t3.put("th-k1", outcome="blocked")
        w.until(lambda: w.job("k1").work_status == "blocked", "k1 blocked in T3")
        time.sleep(0.6)
        assert w.state("k1") == "running", "a blocked T3 run is never reported complete"
        w.settle_reports()
        assert w.posts.shown()[next(c[1] for c in w.posts.sends() if c[2].endswith("code k1"))][-1] == "❓ code k1"
        status, body = w.task(conv, vs, "CONTROL: cancel 2")
        assert status == 200 and body["job"]["state"] == "running" and "being cancelled" in body["reply"], body
        w.until(lambda: w.ledger("stop", "k2"), "k2's waiting turn is stopped")
        assert w.state("k2") == "running", "cancel is confirmed only by T3's own state"
        w.until(lambda: w.state("k2") == "cancelled", "k2 cancelled once T3 shows its run interrupted")
        assert w.t3.get("th-k2")["interrupts"] >= 1 and w.t3.get("th-k2")["outcome"] == "interrupted"
        w.t3.put("th-k1", outcome="completed", messages=[{"role": "assistant", "text": "Answered and done."}])
        w.until(lambda: w.state("k1") == "succeeded", "k1 continues after its block")
        w.release("q1")
        w.until(lambda: w.state("q1") == "succeeded", "q1")
        assert "q2" not in {r["key"] for r in w.ledger("start")}, "a cancelled queued request never starts"
        w.settle_reports()
        texts = sorted(t[-1] for t in w.posts.shown().values())
        assert texts == ["⏹ codewait k2", "✅ code k1"], texts
        assert not any("Hermes task" in t for ts in w.posts.shown().values() for t in ts), "no numbered titles"
        return {"k1": "blocked then completed", "k2": "cancelled from T3", "q2": "cancelled, never started"}
    finally:
        w.close()


def visibility_flags_and_telegram_control():
    w = World(max_concurrent=3).serve()
    try:
        conv, vs = w.call()
        defaults = w.talk.notify_defaults()
        assert defaults == notify.Visibility(large=True, micro=False)
        assert w.task(conv, vs, "hold v1")[0] == 200
        w.release("v1")
        w.until(lambda: w.state("v1") == "succeeded", "v1")
        assert w.task(conv, vs, "codewait v2")[0] == 200
        w.until(lambda: w.job("v2").large, "v2 promoted")
        w.settle_reports()
        assert [c[2] for c in w.posts.calls] == ["⏳ codewait v2"], ("default: only large work posts", w.posts.calls)

        launch = w.talk.inbox.root / "launch.json"
        assert json.loads(launch.read_text())["notify"] == {"large": True, "micro": False}, launch.read_text()
        bot = fx.BOT_TOKEN.split(":")[0]
        text, buttons = thread_gateway.notify_panel(str(w.home), bot, fx.USER_ID, "private", fx.USER_ID)
        assert [b[0] for b in buttons] == ["T3 code work: on", "Quick requests: off"], buttons
        assert thread_gateway.notify_panel(str(w.home), bot, 555, "private", 555) == (None, None), "not a task user"
        assert thread_gateway.notify_panel(str(w.home), "999", fx.USER_ID, "private", fx.USER_ID) == (None, None)
        assert thread_gateway.notify_panel(str(w.home), bot, fx.USER_ID, "group", -100)[1] is None
        assert thread_gateway.notify_switch(str(w.home), bot, fx.OTHER_USER_ID, "private", fx.USER_ID,
                                            "talk_notify:micro") == (None, None), "a tap in another user's chat"
        _, buttons = thread_gateway.notify_switch(str(w.home), bot, fx.USER_ID, "private", fx.USER_ID, buttons[1][1])
        assert [b[0] for b in buttons] == ["T3 code work: on", "Quick requests: on"], buttons
        assert len(w.posts.calls) == 1, "switching sends, edits, starts and cancels nothing"
        assert w.state("v2") == "running" and len(w.ledger("start")) == 2

        assert w.task(conv, vs, "hold v3")[0] == 200
        w.until(lambda: w.state("v3") == "running", "v3")
        w.release("v3")
        w.until(lambda: w.state("v3") == "succeeded", "v3 done")
        w.settle_reports()
        v3 = w.posts.shown()[w.posts.sends()[-1][1]]
        assert v3[0] in ("🕓 hold v3", "⏳ hold v3") and v3[-1] == "✅ hold v3", ("micro on: shown", v3)
        assert not any("hold v1" in c[2] for c in w.posts.calls), "an earlier task is not backfilled"

        thread_gateway.notify_switch(str(w.home), bot, fx.USER_ID, "private", fx.USER_ID, "talk_notify:large")
        assert w.talk.visibility(w.scope()) == notify.Visibility(large=False, micro=True), "micro only"
        assert w.task(conv, vs, "code v4")[0] == 200
        w.until(lambda: w.job("v4").large, "v4 promoted")
        w.t3.finish("th-v2", "completed", "V2 done.")
        w.release("v2")
        w.until(lambda: w.state("v2") == "succeeded", "v2 done")
        w.settle_reports()
        v4_sends = [c for c in w.posts.sends() if c[2].endswith("code v4")]
        assert len(v4_sends) == 1 and w.posts.shown()[v4_sends[0][1]][-1] == "⏳ code v4", \
            ("v4 was shown as a quick request before its promotion; that one message follows it", w.posts.calls)
        assert w.posts.shown()[w.posts.sends()[0][1]][-1] == "✅ codewait v2", "an existing message follows to the end"

        thread_gateway.notify_switch(str(w.home), bot, fx.USER_ID, "private", fx.USER_ID, "talk_notify:micro")
        assert w.talk.visibility(w.scope()) == notify.Visibility(False, False), "all off"
        count = len(w.posts.calls)
        assert w.task(conv, vs, "hold v5")[0] == 200 and w.task(conv, vs, "code v6")[0] == 200
        w.release("v5")
        w.until(lambda: w.state("v5") == "succeeded" and w.job("v6").large, "v5 done, v6 promoted")
        w.settle_reports()
        assert len(w.posts.calls) == count, ("all off: quiet", w.posts.calls[count:])

        thread_gateway.notify_switch(str(w.home), bot, fx.USER_ID, "private", fx.USER_ID, "talk_notify:large")
        restarted = notify.load(w.home, w.scope(), defaults)
        assert restarted == notify.Visibility(True, False), "the switches persist (read again from disk)"
        assert w.task(conv, vs, "hold v7")[0] == 200
        w.release("v7")
        w.until(lambda: w.state("v7") == "running" or w.state("v7") == "succeeded", "v7")
        assert w.task(conv, vs, "code v8")[0] == 200
        w.until(lambda: w.job("v8").large, "v8 promoted")
        w.settle_reports()
        tail = [c[2] for c in w.posts.calls[count:]]
        assert tail == ["⏳ code v8"], ("a hidden quick request that became T3 work (v8) is posted once; open v6 waits "
                                        "for its next status change; finished v5 is never backfilled", tail)
        w.t3.finish("th-v6", "completed", "V6 done.")
        w.until(lambda: w.state("v6") == "succeeded", "v6 done")
        w.settle_reports()
        v6 = [c for c in w.posts.calls[count:] if c[2].endswith("code v6")]
        assert [c[0] for c in v6 if c[0] == "send"] == ["send"] and v6[-1][2] == "✅ code v6", \
            ("open v6 gets its one message at its next change after the switch turned on", v6)
        assert not any(c[2].endswith("hold v5") for c in w.posts.calls), "finished v5 is never backfilled"
        return {"default": "large only", "micro_on": "hold v3 shown", "micro_only": "large hidden",
                "all_off": "quiet", "promoted_hidden_micro": tail}
    finally:
        w.close()


def hook_records_only_real_admissions():
    root = Path(tempfile.mkdtemp(prefix="talk-work-hook-"))
    try:
        home, job = root / "home", "jb_" + "a" * 24
        env = {work.ENV_JOB: job}
        ok = {"ok": True, "thread_id": "th-1", "state": {"outcome": "running", "run": {"count": 1}}}
        assert work.observe("terminal", {"action": "start"}, json.dumps(ok), "s", home, env) is None
        for action in ("status", "projects", "list", "read", "wait", "interrupt", "answer", "settle"):
            assert work.observe("t3_control", {"action": action, "thread_id": "th-1"}, json.dumps(ok), "s", home,
                                env) is None, action
        assert work.observe("t3_control", {"action": "start", "request_id": "r1"}, json.dumps(ok), "s", home, {}) \
            is None, "outside a Talk task nothing is recorded"
        refused = {"ok": False, "error": "rejected", "outcome_unknown": False}
        assert work.observe("t3_control", {"action": "start", "request_id": "r2"}, json.dumps(refused), "s", home,
                            env) is None
        lost = {"ok": False, "error": "transport_error", "outcome_unknown": True, "thread_id": "th-1"}
        first = work.observe("t3_control", {"action": "start", "request_id": "r1"}, json.dumps(ok), "s", home, env)
        again = work.observe("t3_control", {"action": "start", "request_id": "r1"}, json.dumps(lost), "s", home, env)
        assert again == first and len(work.observations(home, job)) == 1, "a retry never downgrades or duplicates"
        gateway_job = "jb_" + "g" * 24
        work.mark_session(home, "sess-gw", gateway_job)
        assert work.observe("t3_control", {"action": "send", "thread_id": "th-9", "request_id": "r9"},
                            json.dumps({"ok": True, "thread_id": "th-9", "delivery": "started", "state": {}}),
                            "sess-gw", home, {})["job_id"] == gateway_job, "a gateway turn is found by its session"
        work.unmark_session(home, "sess-gw", "jb_" + "h" * 24)
        assert work.observe("t3_control", {"action": "send", "thread_id": "th-9", "request_id": "r10"},
                            json.dumps({"ok": True, "thread_id": "th-9"}), "sess-gw", home, {}) is not None, \
            "another job's turn end leaves the marker"
        lineage = {"sess-child": "sess-gw"}
        assert work.observe("t3_control", {"action": "start", "request_id": "r12"},
                            json.dumps({"ok": True, "thread_id": "th-12", "state": {}}), "sess-child", home, {},
                            parent=lambda h, sid: lineage.get(sid))["job_id"] == gateway_job, \
            "a turn compressed into a child session is still found through its parent"
        work.unmark_session(home, "sess-gw", gateway_job)
        assert work.observe("t3_control", {"action": "send", "thread_id": "th-9", "request_id": "r11"},
                            json.dumps({"ok": True, "thread_id": "th-9"}), "sess-gw", home, {}) is None
        return {"recorded": ["start ok", "gateway send"], "ignored": ["reads", "refusal", "outside Talk"]}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def config_contract():
    root = Path(tempfile.mkdtemp(prefix="talk-work-config-"))
    try:
        path = root / "talk.yaml"
        path.write_text(yaml_text(root))
        config = load_config(path)
        assert (config.receipts.large_work, config.receipts.micro_tasks, config.tasks.max_concurrent) == (True, False, 3)
        path.write_text(yaml_text(root, receipts="receipts:\n  large_work: false\n  micro_tasks: true\n"))
        assert (load_config(path).receipts.large_work, load_config(path).receipts.micro_tasks) == (False, True)
        refused = {}
        for tasks_extra, receipts in (("  max_open_per_user: 3\n", ""), ("  max_concurrent: 0\n", ""),
                                      ("", "receipts:\n  micro_tasks: 'yes'\n")):
            path.write_text(yaml_text(root, tasks_extra, receipts))
            try:
                load_config(path)
            except ConfigError as exc:
                refused[(tasks_extra + receipts).strip()] = str(exc)
                continue
            raise AssertionError(f"accepted {tasks_extra!r} {receipts!r}")
        return {"refused": refused}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def rollback_keeps_every_job():
    """The deployed revision's own jobs.load reads every job this version writes, so a rollback drops none."""
    source = subprocess.run(["git", "-C", str(ROOT), "show", f"{ROLLBACK_REVISION}:hermes_talk/jobs.py"],
                            capture_output=True, text=True, check=True).stdout
    root = Path(tempfile.mkdtemp(prefix="talk-work-rollback-"))
    try:
        package = root / "hermes_talk"
        shutil.copytree(ROOT / "hermes_talk", package)
        (package / "jobs.py").write_text(source)
        spec = importlib.util.spec_from_file_location("old_hermes_talk", package / "__init__.py",
                                                      submodule_search_locations=[str(package)])
        old_pkg = importlib.util.module_from_spec(spec)
        sys.modules["old_hermes_talk"] = old_pkg
        spec.loader.exec_module(old_pkg)
        old_jobs = importlib.import_module("old_hermes_talk.jobs")
        base = jobs.new_job("p:tg1:1", "c", "v", 1, "d", 1, "code k", time.time())
        link = work.link_from({"key": "k" * 32, "thread_id": "th", "action": "start", "confirmed": True,
                               "outcome": "running", "run_count": 1, "at": 1.0})
        written = [base, replace(base, parallel=True, state=jobs.RUNNING),
                   replace(base, state=jobs.RUNNING, links=(link,), turn={"state": "succeeded", "at": 1.0}),
                   replace(base, state=jobs.SUCCEEDED, links=(link,), turn={"state": "succeeded", "at": 1.0},
                           result="x"),
                   replace(base, state=jobs.INTERRUPTED, links=(link,), turn={"state": "succeeded", "at": 1.0},
                           unknown=True, error="how it ended is not known")]
        history = MemoryHistory()
        for i, job in enumerate(written):
            job = replace(job, id=f"jb_rollback{i:02d}" + "x" * 10)
            history.set_meta(jobs.job_key(job.id), jobs.dump(job))
            extra = jobs.dump_extra(job)
            if extra:
                history.set_meta(jobs.work_key(job.id), extra)
            assert old_jobs.load(jobs.dump(job)) is not None, ("the deployed Talk drops", job)
        reloaded = {j.id: j for j in (jobs.load(raw, history.get_meta(jobs.work_key(k[len(jobs.JOB_PREFIX):])))
                                       for k, raw in history.list_meta_prefix(jobs.JOB_PREFIX))}
        ordered = sorted(reloaded.values(), key=lambda j: j.id)
        assert [j.large for j in ordered] == [False, False, True, True, True] and ordered[-1].unknown, ordered
        old_unknown = old_jobs.load(jobs.dump(written[-1]))
        assert old_unknown.state == "interrupted" and old_unknown.terminal, "the deployed Talk reads it as finished"
        legacy = json.loads(jobs.dump(base))
        legacy["parallel"] = True  # a record of the undeployed 83cc7dc layout
        assert jobs.load(json.dumps(legacy)).parallel is True
        return {"deployed_revision": ROLLBACK_REVISION, "records_written": len(written), "dropped_by_rollback": 0}
    finally:
        sys.modules.pop("old_hermes_talk", None)
        shutil.rmtree(root, ignore_errors=True)


def runs_are_told_apart():
    """Pure: a link's own run by id, the run count when T3 shows no ids, and nothing concluded from no answer."""
    rec = {"key": "k", "thread_id": "th", "action": "send", "confirmed": True, "delivery": "queued",
           "outcome": "running", "run_count": 1, "active_id": "run-1", "latest_id": "run-1", "at": 1.0}
    link = work.link_from(rec)
    assert link.queued and link.prior_run_id == "run-1" and link.status == "queued"
    read = lambda outcome, count, active, latest, text="": {"ok": True, "state": {"outcome": outcome, "run": {
        "count": count, "active_id": active, "latest_id": latest}}, "messages": [{"role": "assistant", "text": text}]}
    assert work.assess(link, read("running", 1, "run-1", "run-1"), 2).status == "queued", "behind another run"
    assert work.assess(link, read("completed", 1, None, "run-1"), 2).status == "queued", "between runs"
    mine = work.assess(link, read("running", 2, "run-2", "run-2"), 3)
    assert mine.run_id == "run-2" and mine.status == "running"
    assert work.assess(mine, read("completed", 2, None, "run-2", "Mine."), 4).result == "Mine."
    assert work.interruptible(mine, read("running", 2, "run-2", "run-2")) and \
        not work.interruptible(mine, read("running", 3, "run-3", "run-3")), "never interrupt another run"
    started = work.link_from({**rec, "delivery": "started", "active_id": None, "latest_id": "run-1"})
    assert started.prior_run_id == "run-1" and not started.run_id
    assert work.assess(started, read("completed", 2, None, "run-2", "Fast."), 5).status == "completed", \
        "a run that finished before the first read is still recognized"
    plain = work.link_from({**rec, "active_id": None, "latest_id": None})
    noid = lambda outcome, count, text="": {"ok": True, "state": {"outcome": outcome, "run": {"count": count}},
                                            "messages": [{"role": "assistant", "text": text}] if text else []}
    assert work.assess(plain, noid("completed", 1), 2).status == "queued"
    assert work.assess(work.assess(plain, noid("running", 2), 2), noid("completed", 2, "Own."), 3).result == "Own."
    for raw in ({"ok": False, "error": "server_unreachable"}, {"ok": False, "error": "unavailable"}, None):
        assert work.assess(mine, raw, 9) == mine, "an unreadable T3 changes nothing"
    assert work.assess(mine, {"ok": False, "error": "not_found"}, 9).status == work.UNKNOWN
    return {"by_id": "queued, mine, interrupt guard", "by_count": "fallback", "unreadable": "unchanged"}


def a_later_run_is_never_this_runs_outcome():
    """Pure regressions for exact attribution: an earlier run that failed, was interrupted or was never seen to end,
    followed by a later run with unrelated text, is unknown: never completed, never that text. Also by count only."""
    read = lambda outcome, count, active, latest, *msgs: {"ok": True, "state": {"outcome": outcome, "run": {
        "count": count, "active_id": active, "latest_id": latest}}, "messages": [
        {"role": r, "text": t} for r, t in msgs]}
    noid = lambda outcome, count, *msgs: {"ok": True, "state": {"outcome": outcome, "run": {"count": count}},
                                          "messages": [{"role": r, "text": t} for r, t in msgs]}
    start = work.link_from({"key": "k", "thread_id": "th", "action": "start", "confirmed": True, "outcome": "running",
                            "run_count": 1, "active_id": "run-1", "latest_id": "run-1", "at": 1.0})
    assert start.run_id == "run-1"
    later = ("assistant", "Unrelated later run text.")
    seen = {}
    # run-1 failed, or was interrupted, or ended unseen; then run-2 started (live) or finished with unrelated text
    for name, state in (("later live", read("running", 2, "run-2", "run-2", ("user", "next"))),
                        ("later completed", read("completed", 2, None, "run-2", ("user", "next"), later)),
                        ("later failed", read("failed", 2, None, "run-2", later))):
        out = work.assess(start, state, 5)
        seen[name] = out.status
        assert out.status == work.UNKNOWN and out.result == "" and "Unrelated" not in out.error, (name, out)
    # a queued link that never saw its own run: two runs since its admission, none identified as its own
    queued = work.link_from({"key": "q", "thread_id": "th", "action": "send", "confirmed": True, "delivery": "queued",
                             "outcome": "running", "run_count": 1, "active_id": "run-1", "latest_id": "run-1", "at": 1.0})
    out = work.assess(queued, read("completed", 3, None, "run-3", later), 5)
    assert out.status == work.UNKNOWN and out.run_id is None and out.result == "", ("never adopts run-3", out)
    # the same by count only (T3 shows no run ids)
    plain = work.link_from({"key": "p", "thread_id": "th", "action": "start", "confirmed": True, "outcome": "running",
                            "run_count": 1, "active_id": None, "latest_id": None, "at": 1.0})
    for name, state in (("count later live", noid("running", 2, ("user", "next"))),
                        ("count later completed", noid("completed", 2, later)),
                        ("count later failed", noid("failed", 3, later))):
        out = work.assess(plain, state, 5)
        seen[name] = out.status
        assert out.status == work.UNKNOWN and out.result == "", (name, out)
    # idle alone is not success; a newest message that is not the assistant's is no result of this run
    out = work.assess(start, read("idle", 1, None, "run-1", later), 5)
    assert out.status == work.UNKNOWN and out.result == "", ("idle alone", out)
    out = work.assess(start, read("completed", 1, None, "run-1", ("assistant", "Older reply."), ("user", "x")), 5)
    assert out.status == "completed" and out.result == "", ("no older assistant text is borrowed", out)
    for outcome in ("failed", "interrupted"):
        out = work.assess(start, read(outcome, 1, None, "run-1", ("assistant", "Own words.")), 5)
        assert out.status == outcome and out.result == "", (outcome, out)
    return seen


def review_regressions():
    w = World(max_concurrent=1).serve()
    try:
        conv, vs = w.call()
        assert w.task(conv, vs, "hold z1")[0] == 200
        w.release("z1")
        w.until(lambda: w.state("z1") == "succeeded", "z1 done while quick requests are hidden")
        w.settle_reports()
        thread_gateway.notify_switch(str(w.home), fx.BOT_TOKEN.split(":")[0], fx.USER_ID, "private", fx.USER_ID,
                                     "talk_notify:micro")
        w.talk.board.mark_delivered([w.job("z1").id], "context")
        assert w.task(conv, vs, "lostnothread n1")[0] == 200
        w.until(lambda: w.state("n1") == "succeeded", "a lost start without a thread id settles from its turn")
        assert not w.job("n1").large and work.LOST_START in w.job("n1").result, w.job("n1").result
        assert w.task(conv, vs, "code d1")[0] == 200
        w.until(lambda: w.job("d1").large, "d1 promoted")
        (w.root / "t3" / "th-d1.json").unlink()
        w.until(lambda: w.state("d1") == "interrupted", "a deleted thread ends its task as unknown instead of hanging")
        assert w.job("d1").unknown and "no longer has this thread" in w.job("d1").error
        for key in ("r1", "r2"):
            assert w.task(conv, vs, f"codewait {key}")[0] == 200
            w.until(lambda: w.job(key).large, f"{key} promoted")
        w.until(lambda: w.ledger("stop", "r1"), "the oldest live turn of large work is stopped past the bound")
        assert not w.ledger("stop", "r2") and not w.ledger("end", "r2"), "the newer one keeps its turn"
        w.until(lambda: w.job("r1").turn is not None, "r1's stopped turn is recorded")
        assert w.state("r1") == "running" and w.job("r1").turn["state"] == "interrupted", "its T3 work goes on"
        w.t3.finish("th-r1", "completed", "R1 done.")
        w.until(lambda: w.state("r1") == "succeeded", "r1 from T3")
        w.release("r2")
        w.t3.finish("th-r2", "completed", "R2 done.")
        w.until(lambda: w.state("r2") == "succeeded", "r2")
        w.settle_reports()
        history, root, posts = w.history, w.root, w.posts
        w.stop_serving()
        w2 = World(root=root, max_concurrent=1, history=history)
        w2.posts = posts
        w2.serve()
        w = w2
        w.settle_reports()
        texts = [c[2] for c in w.posts.calls]
        assert not any(t.endswith("hold z1") for t in texts), ("a finished hidden task is never backfilled", texts)
        assert sum(1 for c in w.posts.sends() if c[2].endswith("lostnothread n1")) == 1, texts
        return {"backfill": "none after a switch, a save and a restart", "lost_start": "noted", "deleted_thread":
                "unknown", "live_turn_bound": "oldest large turn stopped"}
    finally:
        w.close()


def superseded_run_reports_unknown():
    """End to end: the followed run fails (or is interrupted) and a later run finishes with unrelated text before
    Talk reads the thread again, with run ids and by count only. The task is reported unknown: not completed, no
    result, never that text, in the API, the voice line and the Telegram message."""
    w = World(max_concurrent=3).serve()
    try:
        conv, vs = w.call()
        w.t3.put("th-y1", no_ids=True)  # this thread's T3 shows no run ids: Talk falls back to the run count
        for text in ("code x1", "code y1"):
            assert w.task(conv, vs, text)[0] == 200
        w.until(lambda: w.job("x1").large and w.job("y1").large, "both promoted")
        assert w.job("x1").links[0].run_id and not w.job("y1").links[0].run_id
        unrelated = [{"role": "user", "text": "someone else's message"},
                     {"role": "assistant", "text": "Unrelated later run text."}]
        w.t3.put("th-x1", outcome="completed", count=2, messages=unrelated)  # run-1 ended unseen, run-2 finished
        w.t3.put("th-y1", outcome="completed", count=2, messages=unrelated)
        w.until(lambda: w.job("x1").terminal and w.job("y1").terminal, "both settle")
        listed = {j["delegation_item_id"].split("_")[1]: j for j in w.post("tasks", {"conversation_id": conv,
                                                                                      "voice_session_id": vs})[1]["jobs"]}
        for key in ("x1", "y1"):
            job, public = w.job(key), listed[key]
            assert job.state == "interrupted" and job.unknown and job.result == "", (key, job)
            assert public["outcome_unknown"] is True and public["result"] == "", public
            assert "Unrelated" not in public["spoken"] and "not known" in public["spoken"], public["spoken"]
        status, recall = w.task(conv, vs, f"CONTROL: result {w.job('x1').number}")
        assert status == 200 and "Unrelated" not in recall["reply"] and "not known" in recall["reply"], recall
        w.settle_reports()
        finals = sorted(texts[-1] for texts in w.posts.shown().values())
        assert finals == ["❔ code x1", "❔ code y1"], finals
        return {"x1_by_id": "unknown", "y1_by_count": "unknown", "telegram": finals, "spoken": listed["x1"]["spoken"]}
    finally:
        w.close()


def turn_budget_is_not_a_t3_outcome():
    """A large task's turn that runs past its budget is stopped; the task neither fails nor completes from that."""
    w = World(max_concurrent=1).serve()
    try:
        conv, vs = w.call()
        assert w.task(conv, vs, "codewait b1")[0] == 200
        w.until(lambda: w.job("b1").large, "b1 promoted")
        with w.talk.board.lock:
            job = w.job("b1")
            w.talk.board.jobs[job.id] = replace(job, started=job.started - 100_000)  # its run budget is long past
        w.until(lambda: w.ledger("stop", "b1"), "the turn past its budget is stopped")
        w.until(lambda: w.job("b1").turn is not None, "the stopped turn is recorded")
        assert w.state("b1") == "running" and w.job("b1").turn["state"] == "failed", \
            ("the budget ends only the turn; the T3 work keeps the task open", w.job("b1").turn)
        assert w.job("b1").work_status == "running"
        w.t3.finish("th-b1", "completed", "Budget-safe result.")
        w.until(lambda: w.state("b1") == "succeeded", "b1 settles from T3")
        assert w.job("b1").result == "Budget-safe result." and not w.job("b1").error
        return {"turn": "failed (budget)", "task": "succeeded from T3"}
    finally:
        w.close()


CHECKS = (hook_records_only_real_admissions, runs_are_told_apart, a_later_run_is_never_this_runs_outcome,
          superseded_run_reports_unknown, turn_budget_is_not_a_t3_outcome, config_contract, rollback_keeps_every_job,
          slow_t3_keeps_requests_responsive, promotion_is_exact, other_user_no_crossover,
          restart_and_uncertain_admission, cancel_large_and_queued_and_blocked, visibility_flags_and_telegram_control,
          review_regressions)


def main(argv):
    only = set(argv[1:])
    failures, evidence = 0, {}
    checks = [c for c in CHECKS if not only or c.__name__ in only]
    for check in checks:
        try:
            evidence[check.__name__] = check()
            print(f"PASS [work] {check.__name__}", flush=True)
        except Exception as exc:
            failures += 1
            evidence[check.__name__] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"FAIL [work] {check.__name__}: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}",
                  flush=True)
    print(json.dumps(evidence, indent=1, default=str, ensure_ascii=False))
    print(f"{len(checks) - failures}/{len(checks)} work checks passed")
    print("TALK_WORK_" + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
