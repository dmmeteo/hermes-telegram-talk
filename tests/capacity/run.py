#!/usr/bin/env python3
"""Task capacity gate: three tasks of one call run at once in independent native sessions; the rest wait, uncapped.

  python3 tests/capacity/run.py      prints TALK_CAPACITY_OK

The real Talk server, TalkRuntime, JobBoard, ReportDesk, agent.NativeAgent and spool.py run over HTTP with signed
synthetic initData and the in-memory SessionDB stand-in. Only the `hermes chat` command is replaced, by the bounded
tests/capacity/runner.py, which holds until the check releases it and logs which native session it ran in. No
model, provider, Hermes core or Telegram is used; status messages go to a recording sender.
"""
import http.client
import json
import os
import shutil
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
from hermes_talk import jobs  # noqa: E402
from hermes_talk.agent import NativeAgent, Readiness, process_start  # noqa: E402
from hermes_talk.config import ConfigError, load_config  # noqa: E402
from hermes_talk.history import MemoryHistory  # noqa: E402
from hermes_talk.talk import TalkRuntime  # noqa: E402

RUNNER = ROOT / "tests" / "capacity" / "runner.py"
DEFAULT_TASKS = "tasks:\n  enabled: true\n  user_ids: [{user}]\n  toolsets: [file]\n  workdir: {work}\n"
DESIRED = "  max_concurrent: 3\n"
CAPACITY = {"max_concurrent": 3}  # what DESIRED parses to (desired_yaml_parses_and_bounds_hold)
SHOW_ALL = "receipts:\n  micro_tasks: true\n"  # every task posts, so each one's message is checked


def yaml_text(work, tasks_extra="", receipts=SHOW_ALL):
    base = fx.config_data()
    head = "".join(f"{key}: {json.dumps(value)}\n" for key, value in base.items())
    return head + DEFAULT_TASKS.format(user=fx.USER_ID, work=work) + tasks_extra + receipts


class BoundedAgent(NativeAgent):
    """agent.NativeAgent running tests/capacity/runner.py; readiness, which inspects a Hermes install, is granted."""

    def readiness(self):
        return Readiness(True, "")


class Posts:
    """Records the Telegram status messages Talk sends and edits."""

    def __init__(self):
        self.calls, self.lock = [], threading.Lock()

    def send(self, destination, text):
        with self.lock:
            self.calls.append(("send", str(7000 + len(self.calls)), text))
            return {"success": True, "message_id": self.calls[-1][1]}

    def edit(self, destination, message_id, text):
        with self.lock:
            self.calls.append(("edit", str(message_id), text))
        return {"success": True, "message_id": str(message_id)}

    def shown(self):
        """message id -> texts it showed, in order."""
        with self.lock:
            out = {}
            for _, message_id, text in self.calls:
                out.setdefault(message_id, []).append(text)
            return out


class World:
    def __init__(self, history=None):
        self.root = Path(tempfile.mkdtemp(prefix="talk-capacity-"))
        self.home = fx.write_home(self.root / "home")
        self.work = self.root / "work"
        self.work.mkdir()
        (self.root / "release").mkdir()
        (self.root / "talk.yaml").write_text(yaml_text(self.work))
        config = load_config(self.root / "talk.yaml")
        self.config = replace(config, tasks=replace(config.tasks, **CAPACITY))
        self.history = history or MemoryHistory()
        self.agent = BoundedAgent(self.config.tasks, self.home, command=[sys.executable, str(RUNNER)],
                                  env={**os.environ, "TALK_CAPACITY_DIR": str(self.root)})
        self.posts = Posts()
        self.fake = fx.FakeUpstream()
        self.talk = self.httpd = None

    def serve(self):
        self.talk = TalkRuntime(self.config, fx.BOT_TOKEN, self.history, home=self.home, agent=self.agent,
                                sender=self.posts, skill=fx.file_skill(self.home))
        app = server.VoiceApp(fx.BOT_TOKEN, {fx.USER_ID}, fx.ORIGIN, resolve=self.fake.resolve, post=self.fake.post,
                              talk=self.talk)
        app.limiter.limit = 1000
        for limiter in app.talk_limiters.values():
            limiter.limit = 1000
        self.httpd = server.BoundedServer(("127.0.0.1", 0), server.make_handler(app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def post(self, route, fields):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        body = json.dumps({"initData": fx.sign_init_data(fx.USER_ID, int(time.time())), **fields})
        conn.request("POST", "/tg-voice/api/" + route, body=body,
                     headers={"Content-Type": "application/json", "Origin": fx.ORIGIN})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def call(self):
        status, boot = self.post("bootstrap", {})
        assert status == 200, boot
        status, answer = self.post("session", {"sdp": fx.OFFER, "conversation_id": boot["conversation_id"]})
        assert status == 200, answer
        return boot["conversation_id"], answer["voice_session_id"]

    def task(self, conv, vs, key):
        return self.post("task", {"conversation_id": conv, "voice_session_id": vs, "delegation_item_id": f"del_{key}",
                                  "request": f"hold {key}"})

    def ledger(self, event=None):
        path = self.root / "ledger.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if event is None or r["event"] == event]

    def states(self):
        """key -> job state, as the board stores it."""
        return {j.request.split()[-1]: j.state for j in self.talk.board.jobs.values()}

    def release(self, key):
        (self.root / "release" / key).touch()

    def until(self, predicate, what, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}: states {self.states()}, starts "
                             f"{[r['key'] for r in self.ledger('start')]}")

    def quiet_starts(self, count):
        """The runner has started exactly `count` runs and no further one follows."""
        self.until(lambda: len(self.ledger("start")) >= count, f"{count} starts")
        time.sleep(0.6)
        assert len(self.ledger("start")) == count, [r["key"] for r in self.ledger("start")]

    def started(self, key):
        """The runner's start record for `key`, once it has written one."""
        self.until(lambda: key in {r["key"] for r in self.ledger("start")}, f"{key} started")
        return {r["key"]: r for r in self.ledger("start")}[key]

    def by_state(self, state):
        return sorted(k for k, s in self.states().items() if s == state)

    def close(self):
        for key in "k1 k2 k3 k4 k5 k6 k7 k8 k9 r1 r2 r3 q1 q2 q3 q4".split():
            self.release(key)
        if self.talk is not None and self.talk.board is not None:
            self.talk.board.wait_idle(20)
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        shutil.rmtree(self.root, ignore_errors=True)


def desired_yaml_parses_and_bounds_hold():
    root = Path(tempfile.mkdtemp(prefix="talk-capacity-config-"))
    try:
        path = root / "talk.yaml"
        path.write_text(yaml_text(root, DESIRED))
        tasks = load_config(path).tasks
        assert {"max_concurrent": tasks.max_concurrent} == CAPACITY, tasks
        path.write_text(yaml_text(root))
        tasks = load_config(path).tasks
        assert tasks.max_concurrent == 1 and not hasattr(tasks, "max_open_per_user"), "no open cap exists"
        refused = {}
        for extra in ("  max_concurrent: 0\n", "  max_concurrent: 4\n", "  max_concurrent: '3'\n",
                      "  max_concurrent: true\n", "  max_open_per_user: 8\n"):
            path.write_text(yaml_text(root, extra))
            try:
                load_config(path)
            except ConfigError as exc:
                refused[extra.strip()] = str(exc)
                continue
            raise AssertionError(f"accepted an invalid capacity: {extra!r}")
        return {"desired": CAPACITY, "refused": refused}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def eight_open_three_running_one_call():
    w = World().serve()
    try:
        conv, vs = w.call()
        own = w.history.create_session("Synthetic conversation session", "cli")
        w.talk.store.bind_agent_session(conv, own)
        for n in range(1, 9):
            status, body = w.task(conv, vs, f"k{n}")
            assert status == 200 and body["created"] and body["job"]["number"] == n, (n, status, body)
        w.quiet_starts(3)
        assert w.by_state("running") == ["k1", "k2", "k3"] and w.by_state("accepted") == [f"k{n}" for n in range(4, 9)], \
            w.states()
        listed = {j["delegation_item_id"]: j["state"] for j in w.post("tasks", {"conversation_id": conv,
                                                                                "voice_session_id": vs})[1]["jobs"]}
        assert sorted(listed.values()).count("running") == 3 and sorted(listed.values()).count("accepted") == 5, listed
        starts = {r["key"]: r for r in w.ledger("start")}
        assert starts["k1"]["resume"] == own, "the oldest task continues the conversation's own native session"
        assert starts["k2"]["resume"] == starts["k3"]["resume"] == "", "the others run in fresh native sessions"
        assert len({starts[k]["session"] for k in ("k1", "k2", "k3")}) == 3, starts
        assert len({starts[k]["pid"] for k in ("k1", "k2", "k3")}) == 3, starts

        status, ninth = w.task(conv, vs, "k9")
        assert status == 200 and ninth["created"] and ninth["job"]["state"] == "accepted", (status, ninth)
        assert w.by_state("accepted") == [f"k{n}" for n in range(4, 10)], "a ninth open task waits too: no open cap"
        status, again = w.task(conv, vs, "k2")
        assert status == 200 and not again["created"] and again["job"]["number"] == 2, again

        w.release("k2")
        w.until(lambda: w.states()["k2"] == "succeeded" and w.states()["k4"] == "running", "k2 done, k4 promoted")
        w.quiet_starts(4)
        assert w.by_state("running") == ["k1", "k3", "k4"], w.states()
        assert w.started("k4")["resume"] == "", "k1 still holds the conversation session"
        assert w.talk.store.agent_session(conv) == own, "a parallel task never rebinds the conversation's session"

        k8 = next(j for j in w.talk.board.jobs.values() if j.request == "hold k8")
        assert w.post("task/cancel", {"conversation_id": conv, "job_id": k8.id})[0] == 200
        w.until(lambda: w.states()["k8"] == "cancelled", "queued k8 cancelled")
        k3 = next(j for j in w.talk.board.jobs.values() if j.request == "hold k3")
        assert w.post("task/cancel", {"conversation_id": conv, "job_id": k3.id})[0] == 200
        w.until(lambda: w.states()["k3"] == "cancelled" and w.states()["k5"] == "running", "k3 stopped, k5 promoted")
        assert [r["key"] for r in w.ledger("stop")] == ["k3"], w.ledger("stop")

        w.release("k1")
        w.until(lambda: w.states()["k1"] == "succeeded" and w.states()["k6"] == "running", "k1 done, k6 promoted")
        assert w.started("k6")["resume"] == own, \
            "the next task takes over the conversation's session once it is free"
        for key in ("k4", "k5", "k6"):
            w.release(key)
        w.until(lambda: w.states()["k7"] == "running", "k7 promoted")
        w.release("k7")
        w.release("k9")
        w.until(lambda: not w.by_state("running") and not w.by_state("accepted"), "all settled")
        assert w.by_state("succeeded") == ["k1", "k2", "k4", "k5", "k6", "k7", "k9"] and \
            w.by_state("cancelled") == ["k3", "k8"], w.states()
        started = sorted((j.number, j.started) for j in w.talk.board.jobs.values() if j.started)
        order = [f"k{n}" for n, _ in started]
        assert order == ["k1", "k2", "k3", "k4", "k5", "k6", "k7", "k9"] and \
            all(a[1] <= b[1] for a, b in zip(started, started[1:])), ("FIFO admission", started)
        assert peak(w.ledger()) == 3, peak(w.ledger())
        assert w.talk.store.agent_session(conv) == own
        stored = {j.request.split()[-1]: (j.parallel, json.loads(w.history.get_meta(jobs.job_key(j.id))),
                                          json.loads(w.history.get_meta(jobs.work_key(j.id)) or "{}"))
                  for j in w.talk.board.jobs.values()}
        assert all("parallel" not in d and e.get("parallel", False) is flag for flag, d, e in stored.values()), \
            ("the lane flag lives beside the base record, so an older Talk still loads every job", stored)
        assert {k for k, (flag, _, _) in stored.items() if flag} >= {"k2", "k3", "k4", "k5"} and \
            not any(stored[k][0] for k in ("k1", "k6", "k8")), {k: f for k, (f, _, _) in stored.items()}

        for job in w.talk.board.jobs.values():
            w.talk.desk.wait_settled(job.id, 10)
        shown = w.posts.shown()
        sends = [c for c in w.posts.calls if c[0] == "send"]
        assert len(sends) == 9 and len(shown) == 9, ("one status message per task", w.posts.calls)
        finals = sorted(texts[-1] for texts in shown.values())
        assert finals == sorted([f"✅ hold k{n}" for n in (1, 2, 4, 5, 6, 7, 9)] + ["⏹ hold k3", "⏹ hold k8"]), finals
        return {"running_at_once": ["k1", "k2", "k3"], "queued": ["k4", "k5", "k6", "k7", "k8", "k9"], "ninth": status,
                "sessions": {k: (starts[k]["resume"] == own and "conversation") or "fresh" for k in ("k1", "k2", "k3")},
                "start_order": order, "peak_running": 3, "status_messages": len(shown)}
    finally:
        w.close()


def peak(rows):
    level, best = 0, 0
    for row in sorted(rows, key=lambda r: r["at"]):
        level += 1 if row["event"] == "start" else -1
        best = max(best, level)
    return best


def restart_adopts_three_runs_and_keeps_lanes():
    w = World()
    try:
        history = w.history
        conv, vs = "conv_restart", "vs_restart"
        own = history.create_session("Synthetic conversation session", "cli")
        history.set_meta(f"hermes_talk:agent_session:{conv}", own)
        scope = f"synthetic:b:{fx.BOT_TOKEN.split(':')[0]}:{fx.USER_ID}"
        seeded, now = {}, time.time()

        def seed(n, key, **fields):
            job = jobs.new_job(scope, conv, vs, 1, f"del_{key}", n, f"hold {key}", now + n * 0.01)
            job = replace(job, **fields)
            history.set_meta(jobs.job_key(job.id), jobs.dump(job))
            if jobs.dump_extra(job):
                history.set_meta(jobs.work_key(job.id), jobs.dump_extra(job))
            seeded[key] = job

        for n, (key, resume) in enumerate((("r1", own), ("r2", None), ("r3", None)), start=1):
            process, output = w.agent.start(f"jb_seed_{key}_" + "x" * 12, f"TASK: hold {key}", resume)
            # The Talk that started these runs is gone; init reaps them when they end, as it would after a restart.
            threading.Thread(target=process.wait, daemon=True).start()
            seed(n, key, state=jobs.RUNNING, started=now, pid=process.pid, pid_start=process_start(process.pid),
                 output=output, parallel=resume is None)
        seed(4, "gone", state=jobs.RUNNING, started=now, pid=2 ** 22 + 7, pid_start="0", output=None, parallel=True)
        for n, key in enumerate(("q1", "q2", "q3", "q4"), start=5):
            seed(n, key)
        w.until(lambda: len(w.ledger("start")) == 3, "three seeded runs")
        w.serve()
        w.quiet_starts(3)
        assert w.by_state("running") == ["r1", "r2", "r3"] and w.states()["gone"] == "interrupted", w.states()
        assert w.by_state("accepted") == ["q1", "q2", "q3", "q4"], "a restart adopts the live runs and starts nothing"
        w.release("r2")
        w.until(lambda: w.states()["q1"] == "running", "q1 promoted after r2")
        assert w.started("q1")["resume"] == "", "r1 still holds the conversation session"
        w.release("r1")
        w.until(lambda: w.states()["q2"] == "running", "q2 promoted after r1")
        assert w.started("q2")["resume"] == own
        for key in ("r3", "q1", "q2", "q3", "q4"):
            w.release(key)
        w.until(lambda: not w.by_state("running") and not w.by_state("accepted"), "all settled")
        assert w.by_state("succeeded") == ["q1", "q2", "q3", "q4", "r1", "r2", "r3"], w.states()
        assert [r["key"] for r in w.ledger("start")].count("gone") == 0, "an interrupted run is never rerun"
        assert peak(w.ledger()) == 3
        return {"adopted": ["r1", "r2", "r3"], "interrupted": "gone", "promoted": ["q1 fresh", "q2 conversation"]}
    finally:
        w.close()


CHECKS = (desired_yaml_parses_and_bounds_hold, eight_open_three_running_one_call,
          restart_adopts_three_runs_and_keeps_lanes)


def main():
    failures, evidence = 0, {}
    for check in CHECKS:
        try:
            evidence[check.__name__] = check()
            print(f"PASS [capacity] {check.__name__}", flush=True)
        except Exception as exc:
            failures += 1
            evidence[check.__name__] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"FAIL [capacity] {check.__name__}: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
                  flush=True)
    print(json.dumps(evidence, indent=1, default=str, ensure_ascii=False))
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} capacity checks passed")
    print("TALK_CAPACITY_" + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
