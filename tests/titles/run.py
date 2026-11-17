#!/usr/bin/env python3
"""Title gate: a large voice task's Telegram status message names its objective in one short English sentence.

  python3 tests/titles/run.py      prints TALK_TITLES_OK

The real Talk server, TalkRuntime, JobBoard, ReportDesk and the post_tool_call hook body (work.observe, run inside
each task process) run over HTTP with signed synthetic initData, as in tests/work/run.py. Only `hermes chat` is
replaced (tests/work/runner.py, verb `intent`), T3 by tests/work/fake_t3.py, and the plugin's host ctx.llm by
HostModel below: a synthetic stand-in that answers from a fixed table, so this gate proves the plumbing (what the
model is given, when it is asked, what is posted), never the semantic quality of a real model. All speech, intents
and titles are synthetic. Nothing reaches Telegram, T3 or a provider.
"""
import json
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "work"))

from hermes_talk import receipts  # noqa: E402
from hermes_talk.pulse.model import PulseModel  # noqa: E402
from run import World  # noqa: E402  (tests/work/run.py)

# Synthetic Ukrainian speech: long filler first, the objective later (past the first 60 characters).
SPEECH_MUTE = ("Так... Нє, ну загалом оця от типу апка в принципі працює, ну я подивився, все норм, але от що я "
               "хотів сказати, значить: на аватарці асистента досі видно індикатор мюту, його треба прибрати.")
SPEECH_BARS = ("Окей, окей, я поняв, дякую, добре, значить тепер щодо тепер, ну, того самого треду: для аватара "
               "Retro 95 треба окремі смужки рівня голосу, для мене і для асистента.")
SPEECH_VAGUE = "Ну давай, ага, запускай те, про що ми домовились, і все, дякую."  # no objective in the words at all
SPEECH_LOG = "Слухай, ну, коротше, глянь, будь ласка, що там у логах деплою сталося вночі."

# What the task agent actually admitted to T3 (the work intent), in English as its T3 messages are.
INTENT_MUTE = {"action": "start", "thread": "th-mute", "title": "Avatar mute indicator",
               "message": "Remove the mute indicator from the assistant avatar in the Talk Mini App. [SYN-MUTE]"}
INTENT_BARS = {"action": "send", "thread": "th-mute",  # the reused thread keeps its old title
               "message": "New objective: add separate voice-level bars for the user and the assistant to the "
                          "Retro 95 avatar. [SYN-BARS]"}
INTENT_EXPORT = {"action": "start", "thread": "th-export", "title": "talk-export-1116",
                 "message": "Add a CSV export of the synthetic call history. [SYN-EXPORT]"}
INTENT_LOG = {"action": "start", "thread": "th-log", "message": "Inspect the synthetic deploy log. [SYN-LOG]"}

OBJECTIVES = {"SYN-MUTE": "Remove the mute indicator from the assistant avatar.",
              "SYN-BARS": "Add separate voice-level bars to the Retro 95 avatar.",
              "SYN-EXPORT": "Add a CSV export of the call history.",
              "SYN-LOG": "Inspect last night's deploy log."}
SECRET = "sk-" + "s" * 24
UKRAINIAN = set("абвгґдеєжзиіїйклмнопрстуфхцчшщьюяАБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯ")


class HostModel:
    """The plugin's host ctx.llm as PulseModel calls it: records each title request, answers from OBJECTIVES by
    the synthetic marker it finds in what it was given (nothing to name: an empty title). `fail` raises that many
    times first; `answer` overrides the table."""

    def __init__(self, fail=0, answer=None):
        self.fail, self.answer, self.asked, self.lock = fail, answer, [], threading.Lock()

    def complete_structured(self, instructions, input, json_mode, purpose, timeout, max_tokens):
        text = input[0]["text"]
        with self.lock:
            self.asked.append({"instructions": instructions, "text": text, "purpose": purpose})
            if self.fail:
                self.fail -= 1
                raise TimeoutError("synthetic outage")
        title = self.answer or next((t for marker, t in OBJECTIVES.items() if marker in text), "")
        import types
        return types.SimpleNamespace(parsed={"title": title}, text="", provider="fixture", model="titles")

    def titles_asked(self):
        with self.lock:
            return [a for a in self.asked if a["purpose"] == receipts.TITLE_PURPOSE]


class Route(PulseModel):
    """TalkRuntime.model whose host route can be unbound (as for ~30-60 s after a Talk start) and bound later."""

    def __init__(self, host, bound=True):
        super().__init__()
        self.host, self.bound = host, bound

    @property
    def llm(self):
        return self.host if self.bound else None


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def world(host=None, bound=True, root=None, history=None):
    w = World(max_concurrent=3, root=root, history=history, model=Route(host or HostModel(), bound))
    (w.root / "intents").mkdir(exist_ok=True)
    w.serve()
    w.talk.desk.name_backoff = (0.3,)
    return w


def admit(w, key, intent, speech, title_line=""):
    (w.root / "intents" / f"{key}.json").write_text(json.dumps(intent))
    conv, vs = w.conv
    text = f"{speech} intent {key}" + (f"\nTITLE: {title_line}" if title_line else "")
    status, body = w.task(conv, vs, text, delegation=f"del_{key}_{time.time_ns()}")
    assert status == 200, body
    w.until(lambda: w.job(key).large, f"{key} promoted by its T3 admission")
    return w.job(key)


def shown(w, key, timeout=15):
    """Every text of the task's one message, once its desk has nothing left to do (naming included)."""
    job = w.job(key)
    deadline = time.time() + timeout
    report = None
    while time.time() < deadline:
        report = w.talk.desk.wait_settled(job.id, max(0.1, deadline - time.time()))
        if report is not None and report.message_id and not getattr(report, "naming", False) \
                and not report.needs_edit:
            break
        time.sleep(0.05)
    assert report is not None and report.message_id, (key, report)
    return w.posts.shown()[report.message_id]


def plain_english(text):
    return not (set(text) & UKRAINIAN) and SECRET not in text and not set("*_`[]<>#\\") & set(text)


def filler_speech_named_from_admitted_work():
    host = HostModel()
    w = world(host)
    try:
        w.conv = w.call()
        admit(w, "t1", INTENT_MUTE, SPEECH_MUTE)
        first = shown(w, "t1")
        admit(w, "t2", INTENT_BARS, SPEECH_BARS)
        admit(w, "t4", INTENT_EXPORT, SPEECH_VAGUE)
        second, vague = shown(w, "t2"), shown(w, "t4")
        assert first[-1].endswith(" " + OBJECTIVES["SYN-MUTE"]), first
        assert second[-1].endswith(" " + OBJECTIVES["SYN-BARS"]), ("a reused thread names the new objective", second)
        assert vague[-1].endswith(" " + OBJECTIVES["SYN-EXPORT"]), ("vague speech: the admitted work names it", vague)
        assert all(plain_english(t) for t in first + second + vague), (first, second, vague)
        asked = {a["text"] for a in host.titles_asked()}
        by_marker = {m: [a for a in asked if m in a] for m in ("SYN-MUTE", "SYN-BARS", "SYN-EXPORT")}
        assert all(len(v) == 1 for v in by_marker.values()), ("one title call per task", by_marker)
        bars = by_marker["SYN-BARS"][0]
        assert "Avatar mute indicator" not in bars and "Retro 95" in bars and "смужки рівня голосу" in bars, \
            ("the send's message is the intent; the reused thread's old title is not given", bars)
        mute = by_marker["SYN-MUTE"][0]
        assert "індикатор мюту" in mute and "Avatar mute indicator" in mute, \
            ("the whole request (its objective sits past the first 60 characters) and the start's title", mute)
        assert "Hermes task" not in " ".join(first + second + vague)
        w.t3.finish("th-mute", "completed", "Done.")  # t1's run ends, t2's queued run starts
        w.t3.finish("th-mute", "completed", "Bars added.")
        w.t3.finish("th-export", "completed", "Exported.")
        w.until(lambda: w.state("t2") == "succeeded" and w.state("t4") == "succeeded", "T3 work settles")
        w.settle_reports()
        finals = {k: shown(w, k)[-1] for k in ("t1", "t2", "t4")}
        assert finals["t2"] == "✅ " + OBJECTIVES["SYN-BARS"] and finals["t4"] == "✅ " + OBJECTIVES["SYN-EXPORT"], finals
        assert finals["t1"] == "❔ " + OBJECTIVES["SYN-MUTE"], ("t1's run was superseded: unknown, never done", finals)
        sends = w.posts.sends()
        assert len(sends) == 3, ("one message per large task", w.posts.calls)
        return {"pairs": [{"speech": SPEECH_MUTE, "intent": INTENT_MUTE["message"], "title": first[-1]},
                          {"speech": SPEECH_BARS, "intent": INTENT_BARS["message"], "title": second[-1]},
                          {"speech": SPEECH_VAGUE, "intent": INTENT_EXPORT["message"], "title": vague[-1]}],
                "finals": finals, "title_calls": len(host.titles_asked())}
    finally:
        w.close()


def good_and_invalid_title_lines():
    host = HostModel()
    w = world(host)
    try:
        w.conv = w.call()
        admit(w, "g1", INTENT_LOG, SPEECH_LOG, title_line="Check the deploy log")
        good = shown(w, "g1")
        assert good[-1].endswith(" Check the deploy log") and not host.titles_asked(), \
            ("a usable English TITLE line is the title; no model call", good, host.asked)
        admit(w, "g2", INTENT_LOG, SPEECH_LOG, title_line="Перевірити лог деплою")
        invalid = shown(w, "g2")
        assert invalid[-1].endswith(" " + OBJECTIVES["SYN-LOG"]), ("an unusable TITLE line is named", invalid)
        status, body = w.task(*w.conv, "code e1")  # a short English request names itself, as before
        assert status == 200, body
        w.until(lambda: w.job("e1").large, "e1 promoted")
        english = shown(w, "e1")
        assert english[-1].endswith(" code e1") and len(host.titles_asked()) == 1, (english, host.asked)
        return {"good": good[-1], "invalid_title_line": invalid[-1], "short_english": english[-1],
                "title_calls": len(host.titles_asked())}
    finally:
        w.close()


def route_unbound_at_promotion():
    """The live case: Talk was (re)started and the task was promoted before the host route was bound."""
    host = HostModel()
    w = world(host, bound=False)
    try:
        w.conv = w.call()
        job = admit(w, "u1", INTENT_MUTE, SPEECH_MUTE)
        deadline = time.time() + 10
        while not w.posts.sends() and time.time() < deadline:
            time.sleep(0.05)
        early = [c[2] for c in w.posts.calls]
        assert early, "the status message is not held back while the route is unbound"
        assert all(plain_english(t) for t in early), ("never the speech's words", early)
        assert early[0].endswith(" Avatar mute indicator"), ("the admitted start's own title meanwhile", early)
        w.model.bound = True
        texts = shown(w, "u1", timeout=20)
        assert texts[-1].endswith(" " + OBJECTIVES["SYN-MUTE"]), ("renamed once the route is bound", texts)
        assert len(w.posts.sends()) == 1 and all(c[0] == "edit" for c in w.posts.calls[1:]), w.posts.calls
        report = w.talk.desk.get(job.id)
        assert not report.naming and report.title == OBJECTIVES["SYN-MUTE"], report
        return {"before_route": early, "after_route": texts[-1], "sends": len(w.posts.sends())}
    finally:
        w.close()


def outage_is_bounded_and_honest():
    host = HostModel(fail=10 ** 6)
    logs = Logs()
    logging.getLogger("tg-voice").addHandler(logs)
    w = world(host)
    try:
        w.conv = w.call()
        with_title = admit(w, "o1", INTENT_EXPORT, SPEECH_VAGUE)
        without = admit(w, "o2", INTENT_LOG, SPEECH_LOG)
        a, b = shown(w, "o1", timeout=30), shown(w, "o2", timeout=30)
        tries = receipts.TITLE_ATTEMPTS
        assert len([x for x in host.titles_asked() if "SYN-EXPORT" in x["text"]]) == tries, host.asked
        assert len([x for x in host.titles_asked() if "SYN-LOG" in x["text"]]) == tries, host.asked
        assert a[-1].endswith(" talk-export-1116") and b[-1].endswith(f" Hermes task {without.number}"), \
            ("an outage leaves the admitted start's title, else the task number; never the speech", a, b)
        assert all(plain_english(t) for t in a + b), (a, b)
        report = w.talk.desk.get(with_title.id)
        assert not report.naming and any("title not named" in n for n in report.notes), report
        warned = [l for l in logs.lines if "title" in l.lower() and "not named" in l]
        assert warned and not any(SPEECH_VAGUE[:12] in l or "SYN-" in l for l in logs.lines), logs.lines
        return {"tries": tries, "shown": [a[-1], b[-1]], "warning": warned[-1]}
    finally:
        logging.getLogger("tg-voice").removeHandler(logs)
        w.close()


def restart_keeps_the_naming_input():
    host = HostModel()
    w = world(host, bound=False)
    try:
        w.conv = w.call()
        job = admit(w, "r1", INTENT_MUTE, SPEECH_MUTE)
        deadline = time.time() + 10
        while not w.posts.sends() and time.time() < deadline:
            time.sleep(0.05)
        history, root, posts = w.history, w.root, w.posts
        w.stop_serving()
        w.talk.board.follow_s = 3600
        w.talk.desk.dispatch = w.talk.desk._later = lambda *_: None  # the old process has exited: no more steps
        old, w.talk = w.talk, None
        w2 = World(root=root, max_concurrent=3, history=history, model=Route(host, True))
        w2.posts = posts
        w2.serve()
        w2.talk.desk.name_backoff = (0.3,)
        old.board.jobs.clear()
        w = w2
        texts = shown(w, "r1", timeout=20)
        assert texts[-1].endswith(" " + OBJECTIVES["SYN-MUTE"]), texts
        asked = host.titles_asked()
        assert asked and all("індикатор мюту" in a["text"] and "SYN-MUTE" in a["text"] for a in asked), \
            ("after a restart the model still gets the whole request and the admitted work", asked)
        assert len(w.posts.sends()) == 1, ("nothing is resent", w.posts.calls)
        before = len(host.titles_asked())
        w.talk.desk.reconcile()
        w.talk.board._report(w.job("r1"))
        time.sleep(0.5)
        assert len(host.titles_asked()) == before and len(w.posts.sends()) == 1, "a named title is never asked again"
        return {"after_restart": texts[-1], "title_calls": len(asked), "sends": 1, "job": job.number}
    finally:
        w.close()


def redaction_and_plain_text():
    host = HostModel(answer=f"Rotate the *signing* key {SECRET}.")
    w = world(host)
    try:
        w.conv = w.call()
        intent = {**INTENT_LOG, "message": f"Rotate the signing key {SECRET} on the synthetic box. [SYN-LOG]"}
        admit(w, "s1", intent, f"{SPEECH_LOG} ключ {SECRET}")
        texts = shown(w, "s1")
        assert all(SECRET not in a["text"] for a in host.asked), "the model never sees a secret"
        assert texts[-1].endswith(" Rotate the signing key redacted.") and all(plain_english(t) for t in texts), texts
        host.answer = "Перевірити ключ"  # not English: not used
        admit(w, "s2", INTENT_LOG, SPEECH_LOG)
        other = shown(w, "s2", timeout=30)
        assert other[-1].endswith(f" Hermes task {w.job('s2').number}"), other
        return {"redacted": texts[-1], "non_english_answer": other[-1]}
    finally:
        w.close()


def mini_tasks_summarize_instead_of_echoing():
    from hermes_talk import notify
    class SlowHost(HostModel):
        def __init__(self):
            super().__init__(answer="Explain synthetic exports.")
            self.entered, self.release = threading.Event(), threading.Event()

        def complete_structured(self, **kwargs):
            self.entered.set()
            assert self.release.wait(10), "synthetic title release"
            return super().complete_structured(**kwargs)

    host = SlowHost()
    w = world(host)
    try:
        w.conv = w.call()
        notify.toggle(w.home, w.scope(), notify.MICRO, w.talk.notify_defaults())
        request = "Well okay about exports quick mfast"
        status, body = w.task(*w.conv, request)
        assert status == 200, body
        w.until(lambda: w.job("mfast").terminal, "fast request finishes without waiting for naming")
        assert host.entered.wait(1), ("conversational English must be summarized", w.posts.shown())
        assert not w.posts.sends(), "the first title attempt precedes the one send"
        host.release.set()
        fast = shown(w, "mfast")
        status, body = w.task(*w.conv, "Well okay about exports hold mrun")
        assert status == 200, body
        w.until(lambda: w.state("mrun") == "running", "ordinary micro request runs")
        running = shown(w, "mrun")
        w.release("mrun")
        w.until(lambda: w.job("mrun").terminal, "ordinary micro request completes")
        final = shown(w, "mrun")
        assert fast == ["🕓 Explain synthetic exports.", "⏳ Explain synthetic exports.",
                        "✅ Explain synthetic exports."], fast
        assert running[-1] == "⏳ Explain synthetic exports." and final[-1] == "✅ Explain synthetic exports.", final
        assert len(w.posts.sends()) == 2 and w.job("mfast").request == request, (w.posts.calls, w.job("mfast"))
        assert all(not w.job(k).large for k in ("mfast", "mrun"))
        return {"fast": fast, "ordinary": final, "sends": len(w.posts.sends())}
    finally:
        host.release.set()
        w.close()


def mini_retry_uses_completed_topic():
    from hermes_talk import notify
    host = HostModel(answer="Explain synthetic exports.")
    w = world(host, bound=False)
    try:
        w.conv = w.call()
        notify.toggle(w.home, w.scope(), notify.MICRO, w.talk.notify_defaults())
        status, body = w.task(*w.conv, "Ну гаразд, поясни це ще раз quick topic")
        assert status == 200, body
        w.until(lambda: w.job("topic").terminal, "quick task finishes while route is unbound")
        w.talk.model.bound = True
        texts = shown(w, "topic")
        assert texts[-1] == "✅ Explain synthetic exports.", texts
        asked = host.titles_asked()
        assert asked and "done topic" in asked[-1]["text"], ("the finished mini-task adds topic context", asked)
        assert len(w.posts.sends()) == 1 and not w.job("topic").large
        return {"texts": texts, "input": asked[-1]["text"]}
    finally:
        w.close()


CHECKS = [mini_tasks_summarize_instead_of_echoing, mini_retry_uses_completed_topic, filler_speech_named_from_admitted_work, good_and_invalid_title_lines, route_unbound_at_promotion,
          outage_is_bounded_and_honest, restart_keeps_the_naming_input, redaction_and_plain_text]


def main(argv):
    only = set(argv[1:])
    failures, evidence = 0, {}
    checks = [c for c in CHECKS if not only or c.__name__ in only]
    for check in checks:
        try:
            evidence[check.__name__] = check()
            print(f"PASS [titles] {check.__name__}", flush=True)
        except Exception as exc:
            failures += 1
            evidence[check.__name__] = {"error": f"{type(exc).__name__}: {exc}"[:1500]}
            print(f"FAIL [titles] {check.__name__}: {type(exc).__name__}: {str(exc)[:1500]}\n"
                  f"{traceback.format_exc()[-1500:]}", flush=True)
    print(json.dumps(evidence, indent=1, default=str, ensure_ascii=False))
    print(f"{len(checks) - failures}/{len(checks)} title checks passed")
    print("TALK_TITLES_" + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
