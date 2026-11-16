#!/usr/bin/env python3
"""Focused regression for Telegram task status messages (hermes_talk/receipts.py), offline and without Hermes.

  python3 tests/reports/run.py               prints TALK_REPORTS_OK

One message per task, created once and then edited: its text is a status mark and a short English title,
never the result. The real ReportDesk runs against an in-memory SessionDB stand-in and a scripted sender, and
the real TelegramSender against a local fake Bot API over real sockets. Nothing contacts Telegram.
"""
import http.server
import json
import os
import sys
import threading
import time
import traceback
import types
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hermes_talk import jobs, receipts, work  # noqa: E402
from hermes_talk.history import MemoryHistory  # noqa: E402
from hermes_talk.pulse.model import PulseModel  # noqa: E402

USER, BOT = 1001, "123456"
TOKEN = BOT + ":" + "t" * 35
SCOPE = f"synthetic:b:{BOT}:{USER}"
PRIVATE = asdict(receipts.Destination.private(USER, BOT))
TERMINAL_STATES = ("sent", "failed", "uncertain")


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class ScriptedSender:
    """Answers send/edit from a script of outcomes; records every call. "ok" is the default."""

    def __init__(self, *script):
        self.script, self.calls, self.lock = list(script), [], threading.Lock()
        self.gate = None

    def _next(self, call):
        with self.lock:
            outcome = self.script.pop(0) if self.script else "ok"
            call["outcome"] = outcome
            self.calls.append(call)
        if self.gate is not None:
            self.gate.wait(5)
        return outcome

    def send(self, destination, text):
        outcome = self._next({"kind": "send", "chat_id": destination.chat_id, "thread_id": destination.thread_id,
                              "text": text})
        if outcome == "ok":
            return {"success": True, "message_id": str(7000 + len(self.calls))}
        return {"error": {"timeout": "Telegram send failed: Timed out",
                          "refused": "Telegram send failed: httpx.ConnectError: [Errno 111] Connection refused",
                          "chat_not_found": "Telegram send failed: Chat not found"}[outcome]}

    def edit(self, destination, message_id, text):
        outcome = self._next({"kind": "edit", "chat_id": destination.chat_id, "message_id": message_id,
                              "text": text})
        if outcome == "ok":
            return {"success": True, "message_id": message_id}
        return {"error": {"timeout": "Telegram edit failed: Timed out",
                          "not_modified": "Telegram edit failed: Message is not modified: specified new message "
                                          "content and reply markup are exactly the same",
                          "gone": "Telegram edit failed: Message to edit not found",
                          "flood": "Telegram edit failed: Flood control exceeded. Retry in 0 seconds",
                          "flood40": "Telegram edit failed: Flood control exceeded. Retry in 40 seconds"}[outcome]}

    def kinds(self):
        return [c["kind"] for c in self.calls]


def job(number=1, request="check the synthetic build", title="", created=None, report_to=PRIVATE):
    return jobs.new_job(SCOPE, "conv_1", "vs_1", 1, f"del_{number}", number, request,
                        created if created is not None else 2_000_000.0, report_to, title)


def desk_for(history, sender, clock=None, **kw):
    return receipts.ReportDesk(history, sender, clock or Clock(), **{"attempts": 3, "backoff": (0.0, 0.0),
                                                                     "name_backoff": (0.0,), **kw})


def settle(desk, job_id, timeout=5):
    report = desk.wait_settled(job_id, timeout)
    assert report is not None and report.state in TERMINAL_STATES, report
    return report


def walk(desk, j, *states, settle_each=True):
    """Persist the job through `states` the way JobBoard does: each saved change is followed."""
    for state in states:
        j = j.to(state) if state != j.state else j
        desk.follow(j)
        if settle_each:
            settle(desk, j.id)
    return j


def titles():
    take = receipts.take_title
    assert take("check the flight\nTITLE: Check Oslo flights") == ("check the flight", "Check Oslo flights")
    assert take("прочитай почту\nTITLE: Read inbox\n") == ("прочитай почту", "Read inbox")
    assert take("TITLE: Read inbox\nпрочитай почту") == ("TITLE: Read inbox\nпрочитай почту", ""), \
        "only a trailing TITLE line is the voice model's; one inside the task is the user's text"
    assert take("Create a Jira issue.\nTitle: Fix login crash\nTITLE: File Jira issue") == \
        ("Create a Jira issue.\nTitle: Fix login crash", "File Jira issue"), "the user's own Title: field stays"
    assert take("alpha\nTITLE: Alpha task\nREPORT TO: ops") == ("alpha\nREPORT TO: ops", "Alpha task")
    assert take("alpha\nREPORT TO: ops\nTITLE: Alpha task") == ("alpha\nREPORT TO: ops", "Alpha task")
    assert take("cancel task 3\nTITLE: Cancel task") == ("cancel task 3", "Cancel task")
    assert take("найди рейс\nTITLE: Найти рейс") == ("найди рейс", ""), "a non-English title is not used"
    assert take("x\nTITLE: " + "word " * 12) == ("x", ""), "more than a few words is not a title"
    assert take("x\nTITLE: Fix *bold* `code` [link]") == ("x", "Fix bold code link")
    assert take("no directive here") == ("no directive here", "")
    usable = receipts.usable_title
    assert usable("Add separate voice-level bars to the Retro 95 avatar.") == \
        "Add separate voice-level bars to the Retro 95 avatar.", "a whole sentence with a name and a number"
    assert usable('"Remove the mute indicator from the assistant avatar ."') == \
        "Remove the mute indicator from the assistant avatar.", "quotes and a space before the period go"
    assert usable("Summarize the quarterly synthetic infrastructure cost report for the operations team") == "", \
        "past ten words it is not a title, and it is never cut into one"
    assert usable("Synchronize " + "x" * 75) == "", "past 80 characters it is not a title"
    assert usable("Перевірити збірку") == usable("") == usable(None) == ""
    fallback = receipts.job_title
    assert fallback(job(title="Check build"), str) == ("Check build", True)
    assert fallback(job(request="check the synthetic build"), str) == ("check the synthetic build", True)
    assert fallback(job(number=4, request="перевір збірку"), str) == ("Hermes task 4", False), \
        "until the host model names it, another language shows the task number, never the speech"
    filler = "Okay, okay, so, um, now about that thing, you know, the avatar should not show the mute badge any more"
    assert fallback(job(number=5, request=filler), str) == ("Hermes task 5", False), "long English speech too"
    start = work.Link(key="k1", thread_id="th-1", via="start", confirmed=True, intent="Remove the badge.",
                      intent_title="Avatar mute badge")
    send = work.Link(key="k2", thread_id="th-1", via="send", confirmed=True, intent="New objective: add bars.")
    assert fallback(replace(job(number=6, request=filler), links=(start,)), str) == ("Avatar mute badge", False), \
        "the admitted start's own title meanwhile"
    source = receipts.name_source(replace(job(number=6, request=filler), links=(start, send)), str)
    assert source.index("Avatar mute badge") < source.index("New objective: add bars.") < source.index(filler), source
    assert source.count("Avatar mute badge") == 1, "a send's own message is its intent, not the thread's old title"
    secret = "sk-" + "s" * 24
    redact = lambda t: t.replace(secret, "[redacted]")
    assert secret not in fallback(job(request=f"rotate {secret}"), redact)[0]
    assert secret not in receipts.name_source(replace(job(request=f"ротація {secret}"), links=(
        replace(start, intent=f"Rotate {secret}."),)), redact)
    return {"directive": "TITLE: <few English words>", "final": "a usable TITLE line or a short English request",
            "provisional": "the admitted T3 start's title, else Hermes task N", "max_words": receipts.TITLE_WORDS,
            "max_chars": receipts.TITLE_CHARS}


class HostModel:
    """The plugin's host ctx.llm as a report sees it through PulseModel: a synthetic stand-in (no provider)."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def complete_structured(self, instructions, input, json_mode, purpose, timeout, max_tokens):
        self.asked.append({"instructions": instructions, "text": input[0]["text"], "purpose": purpose})
        answer = self.answers.pop(0) if self.answers else {"title": "Check the synthetic build"}
        if isinstance(answer, Exception):
            raise answer
        return types.SimpleNamespace(parsed=answer, text="", provider="fixture", model="titles")


def namer(host):
    return lambda request: receipts.ask_title(PulseModel(llm=host), request)


def other_language_named_in_english():
    ukrainian = "перевір синтетичну збірку і скажи, чи все гаразд"
    host, history, sender = HostModel(), MemoryHistory(), ScriptedSender()
    desk = desk_for(history, sender, namer=namer(host))
    j = walk(desk, job(number=4, request=ukrainian), jobs.ACCEPTED, jobs.RUNNING, jobs.SUCCEEDED)
    assert [c["text"] for c in sender.calls] == ["🕓 Check the synthetic build", "⏳ Check the synthetic build",
                                                  "✅ Check the synthetic build"], sender.calls
    assert [(a["text"], a["purpose"]) for a in host.asked] == [(receipts.name_source(j, str), receipts.TITLE_PURPOSE)], \
        "the host model is asked once, before the only send"
    assert ukrainian in host.asked[0]["text"], "with the whole request"
    assert receipts.load(history.get_meta(receipts.report_key(j.id))).title == "Check the synthetic build"
    sender.calls.clear()
    walk(desk, job(number=5, request="перевір інше"), jobs.ACCEPTED, jobs.RUNNING, jobs.FAILED)
    assert [c["text"] for c in sender.calls] == ["🕓 Check the synthetic build", "⏳ Check the synthetic build",
                                                  "❌ Check the synthetic build"], sender.calls
    asked = len(host.asked)
    for english in (job(number=6, request="check the synthetic build"), job(number=7, request="перевір", title="Check it")):
        walk(desk, english, jobs.ACCEPTED)
    assert len(host.asked) == asked, "an English request or a voice TITLE needs no model"
    secret = "sk-" + "s" * 24
    redact = lambda t: t.replace(secret, "[redacted]")
    cases = {"outage": TimeoutError("synthetic outage"), "not English": {"title": "Перевірити збірку"},
             "too long": {"title": "word " * 12}, "not text": {"title": 7}, "empty": {}}
    shown = {}
    for n, (name, answer) in enumerate(cases.items(), start=10):
        history, sender, host = MemoryHistory(), ScriptedSender(), HostModel(answer)
        desk = desk_for(history, sender, namer=namer(host), redact=redact)
        walk(desk, job(number=n, request=f"перевір {name}"), jobs.ACCEPTED)
        shown[name] = [c["text"] for c in sender.calls]
        assert shown[name] == [f"🕓 Hermes task {n}", "🕓 Check the synthetic build"] and \
            sender.kinds() == ["send", "edit"] and len(host.asked) == 2, \
            ("an unusable first answer: the number is sent, then the retry's title renames it", name, sender.calls)
    history, sender, host = MemoryHistory(), ScriptedSender(), HostModel(*[TimeoutError("down")] * 9)
    desk = desk_for(history, sender, namer=namer(host))
    j = walk(desk, job(number=30, request="перевір завжди недоступне"), jobs.ACCEPTED, jobs.RUNNING)
    report = receipts.load(history.get_meta(receipts.report_key(j.id)))
    assert [c["text"] for c in sender.calls] == ["🕓 Hermes task 30", "⏳ Hermes task 30"] and \
        len(host.asked) == receipts.TITLE_ATTEMPTS and not report.naming and report.notes[-1].startswith(
            "title not named"), ("a lasting outage: bounded attempts, the number stays, a note says why", sender.calls,
                                 len(host.asked), report)
    history, sender, host = MemoryHistory(), ScriptedSender(), HostModel({"title": f"Rotate *key* {secret}"})
    desk = desk_for(history, sender, namer=namer(host), redact=redact)
    walk(desk, job(number=20, request=f"зміни ключ {secret}"), jobs.ACCEPTED)
    assert secret not in host.asked[0]["text"] and sender.calls[0]["text"] == "🕓 Rotate key redacted", \
        (host.asked, sender.calls)
    return {"named": "Check the synthetic build", "model_calls_per_task": 1, "retried": shown,
            "lasting_outage": {"attempts": len(host.asked), "shown": sender.calls[-1]["text"]}}


class GatedHost(HostModel):
    """Answers only once released, so the job moves on while its title is being named."""

    def __init__(self, *answers):
        super().__init__(*answers)
        self.gate = threading.Event()

    def complete_structured(self, **kw):
        self.gate.wait(5)
        return super().complete_structured(**kw)


def every_status_survives_naming():
    for states, marks in (((jobs.RUNNING, jobs.SUCCEEDED), "🕓⏳✅"), ((jobs.CANCELLED,), "🕓⏹"),
                          ((jobs.RUNNING, jobs.FAILED), "🕓⏳❌")):
        host, history, sender = GatedHost(), MemoryHistory(), ScriptedSender()
        desk = desk_for(history, sender, namer=namer(host))
        j = walk(desk, job(number=3, request="перевір синтетичну збірку"), jobs.ACCEPTED, *states, settle_each=False)
        time.sleep(0.2)
        assert not sender.calls, "nothing is sent before the title"
        host.gate.set()
        report = settle(desk, j.id)
        deadline = time.time() + 5
        while (report.needs_edit or desk.passed.get(j.id)) and time.time() < deadline:
            time.sleep(0.01)
            report = desk.get(j.id)
        assert [c["text"] for c in sender.calls] == [f"{m} Check the synthetic build" for m in marks], sender.calls
        assert sender.kinds() == ["send"] + ["edit"] * (len(marks) - 1), sender.calls
    host, history, sender = GatedHost(), MemoryHistory(), ScriptedSender()
    sender.gate = threading.Event()
    desk = desk_for(history, sender, namer=namer(host))
    j = walk(desk, job(number=3, request="перевір синтетичну збірку"), jobs.ACCEPTED, jobs.RUNNING, settle_each=False)
    host.gate.set()
    deadline = time.time() + 5
    while not sender.calls and time.time() < deadline:
        time.sleep(0.01)
    desk.follow(j.to(jobs.SUCCEEDED))  # while the first send is in flight and "running" still waits its turn
    sender.gate.set()
    report = settle(desk, j.id)
    while (report.needs_edit or desk.passed.get(j.id)) and time.time() < deadline:
        time.sleep(0.01)
        report = desk.get(j.id)
    assert [c["text"][0] for c in sender.calls] == ["🕓", "⏳", "✅"], sender.calls
    return {"shown_after_naming": ["queued, running, completed", "queued, cancelled", "queued, running, failed"],
            "status_during_first_send": "queued, running, completed"}


def naming_survives_a_restart():
    ukrainian = "перевір синтетичну збірку"
    history = MemoryHistory()
    j = job(number=8, request=ukrainian)
    title, final = receipts.job_title(j, str)
    left = receipts.Report(job_id=j.id, scope=SCOPE, destination=PRIVATE, title=title, status="queued",
                           naming=not final, created=1.0)
    history.set_meta(receipts.report_key(j.id), json.dumps(asdict(left)))
    host, sender = HostModel(), ScriptedSender()
    desk = desk_for(history, sender, namer=namer(host))
    desk.follow(j)
    desk.reconcile()
    settle(desk, j.id)
    assert [c["text"] for c in sender.calls] == ["🕓 Check the synthetic build"], sender.calls
    assert [a["text"] for a in host.asked] == [receipts.name_source(j, str)] and ukrainian in host.asked[0]["text"], \
        ("a record stored before the naming input was: the input is rebuilt from the job", host.asked)
    again = ScriptedSender()
    desk = desk_for(history, again, namer=namer(HostModel()))
    desk.reconcile()
    time.sleep(0.3)
    assert not again.calls and desk.get(j.id).title == "Check the synthetic build", again.calls
    return {"named_after_restart": True, "sent": 1}


def one_message_lifecycle():
    history, sender = MemoryHistory(), ScriptedSender()
    desk = desk_for(history, sender)
    j = walk(desk, job(title="Check synthetic build"), jobs.ACCEPTED, jobs.RUNNING, jobs.SUCCEEDED)
    report = desk.get(j.id)
    assert sender.kinds() == ["send", "edit", "edit"], sender.calls
    assert [c["text"] for c in sender.calls] == ["🕓 Check synthetic build", "⏳ Check synthetic build",
                                                  "✅ Check synthetic build"], sender.calls
    assert {c.get("message_id") for c in sender.calls[1:]} == {report.message_id}, "every edit targets the one message"
    assert report.state == "sent" and report.shown == report.status == "completed"
    stored = receipts.load(history.get_meta(receipts.report_key(j.id)))
    assert stored.message_id == report.message_id and stored.title == "Check synthetic build", stored
    for state, mark in ((jobs.FAILED, "❌"), (jobs.CANCELLED, "⏹"), (jobs.INTERRUPTED, "❌")):
        sender.calls.clear()
        k = walk(desk, job(number=5, title="Other task"), jobs.ACCEPTED, jobs.RUNNING)
        k = replace(k, error="synthetic failure detail")
        walk(desk, k, state)
        assert sender.calls[-1]["text"] == f"{mark} Other task", sender.calls
        assert all("synthetic failure detail" not in c["text"] for c in sender.calls), "no detailed body"
    desk.follow(replace(j, notes=j.notes + ("adopted",)))
    time.sleep(0.2)
    assert len([c for c in sender.calls if c["text"].endswith("Check synthetic build")]) == 0, \
        "a save that does not change the status edits nothing"
    return {"calls": ["send queued", "edit running", "edit completed"], "message_id": report.message_id}


def coalesced_transitions():
    history, sender = MemoryHistory(), ScriptedSender()
    sender.gate = threading.Event()
    desk = desk_for(history, sender)
    j = job(title="Quick task")
    desk.follow(j)
    deadline = time.time() + 5
    while not sender.calls and time.time() < deadline:
        time.sleep(0.01)
    j = walk(desk, j, jobs.RUNNING, jobs.SUCCEEDED, settle_each=False)
    sender.gate.set()
    report = settle(desk, j.id)
    deadline = time.time() + 5
    while desk.get(j.id).shown != "completed" and time.time() < deadline:
        time.sleep(0.01)
    assert sender.kinds() == ["send", "edit"] and sender.calls[-1]["text"] == "✅ Quick task", sender.calls
    assert desk.get(j.id).shown == "completed" and report.message_id
    return {"transitions_during_send": 2, "edits": 1}


def uncertain_create_never_resent():
    history, sender = MemoryHistory(), ScriptedSender("timeout")
    desk = desk_for(history, sender)
    j = walk(desk, job(title="Ambiguous task"), jobs.ACCEPTED)
    assert desk.get(j.id).state == "uncertain" and sender.kinds() == ["send"]
    walk(desk, j, jobs.RUNNING, jobs.SUCCEEDED)
    time.sleep(0.2)
    assert sender.kinds() == ["send"], "no second message and no edit without a known message"
    restarted = desk_for(history, ScriptedSender())
    restarted.reconcile()
    time.sleep(0.2)
    assert not restarted.sender.calls and restarted.get(j.id).status == "completed"
    return {"after_timeout": "uncertain", "later_transitions_sent": 0, "after_restart_sent": 0}


def refused_create_retries_then_fails():
    history, sender = MemoryHistory(), ScriptedSender("refused", "ok")
    desk = desk_for(history, sender)
    j = walk(desk, job(title="Retry task"), jobs.ACCEPTED)
    report = desk.get(j.id)
    assert report.state == "sent" and sender.kinds() == ["send", "send"] and report.attempts == 0, report
    sender = ScriptedSender("refused", "refused", "refused")
    desk = desk_for(MemoryHistory(), sender)
    k = walk(desk, job(title="Doomed task"), jobs.ACCEPTED)
    assert desk.get(k.id).state == "failed" and "gave up" in desk.get(k.id).error and len(sender.calls) == 3
    sender = ScriptedSender("chat_not_found")
    desk = desk_for(MemoryHistory(), sender)
    k = walk(desk, job(title="Lost chat"), jobs.ACCEPTED)
    assert desk.get(k.id).state == "failed" and len(sender.calls) == 1
    return {"refused_then_ok": 2, "exhausted_after": 3, "chat_not_found": "failed once"}


def edits_converge():
    history, sender = MemoryHistory(), ScriptedSender("ok", "timeout", "ok", "not_modified")
    desk = desk_for(history, sender)
    j = walk(desk, job(title="Edit task"), jobs.ACCEPTED, jobs.RUNNING)
    assert sender.kinds() == ["send", "edit", "edit"] and desk.get(j.id).shown == "running", sender.calls
    j = walk(desk, j, jobs.SUCCEEDED)
    assert desk.get(j.id).shown == "completed", "Telegram's 'not modified' means the message already shows it"
    sender = ScriptedSender("ok", "gone")
    desk = desk_for(MemoryHistory(), sender)
    k = walk(desk, job(title="Deleted message"), jobs.ACCEPTED, jobs.RUNNING)
    held = desk.get(k.id)
    assert held.shown == "queued" and held.held == "running" and len(sender.calls) == 2, held
    walk(desk, k, jobs.SUCCEEDED)
    assert sender.kinds() == ["send", "edit", "edit"] and desk.get(k.id).shown == "completed"
    assert sender.kinds().count("send") == 1, "an edit failure never sends a second message"
    sender = ScriptedSender("ok", "timeout", "timeout", "timeout", "ok")
    desk = desk_for(MemoryHistory(), sender, backoff=(0.0, 1000.0))
    k = walk(desk, job(title="Fresh budget"), jobs.ACCEPTED)
    k = k.to(jobs.RUNNING)
    desk.follow(k)
    deadline = time.time() + 5
    while desk.get(k.id).attempts < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert desk.get(k.id).attempts == 2 and desk.get(k.id).shown == "queued", "running edit waits after two timeouts"
    walk(desk, k, jobs.SUCCEEDED)
    assert desk.get(k.id).shown == "completed", "the final status gets its own retry budget, not running's leftovers"
    history = MemoryHistory()
    sender = ScriptedSender("ok", "flood", "flood", "flood")
    desk = desk_for(history, sender)
    k = walk(desk, job(title="Flooded"), jobs.ACCEPTED, jobs.RUNNING)
    stalled = desk.get(k.id)
    assert stalled.shown == "queued" and stalled.held == "running" and stalled.stalled, stalled
    restarted = desk_for(history, ScriptedSender())
    restarted.reconcile()
    assert settle(restarted, k.id).shown == "running", "a restart retries an edit that gave up on a temporary error"
    gone = desk_for(history, ScriptedSender("ok", "gone"))
    k2 = walk(gone, job(number=7, title="Gone"), jobs.ACCEPTED, jobs.RUNNING)
    again = desk_for(history, ScriptedSender())
    again.reconcile()
    time.sleep(0.2)
    assert not again.sender.calls and again.get(k2.id).held == "running", "a refused edit is not retried by a restart"
    wait = receipts.retry_after("Telegram edit failed: Flood control exceeded. Retry in 40 seconds")
    assert wait == 40.0 and receipts.retry_after("Timed out") is None
    clock = Clock()
    flood = desk_for(MemoryHistory(), ScriptedSender("ok", "flood40"), clock)
    k4 = walk(flood, job(title="Flood wait", created=clock.now + 1), jobs.ACCEPTED).to(jobs.RUNNING)
    flood.follow(k4)
    deadline = time.time() + 5
    while not flood.get(k4.id).error and time.time() < deadline:
        time.sleep(0.01)
    flooded = flood.get(k4.id)
    assert flooded.not_before >= clock.now + 40 and flooded.shown == "queued", ("Telegram's flood wait is honored", flooded)
    odd = desk_for(MemoryHistory(), ScriptedSender())
    odd.sender.edit = lambda destination, message_id, text: None
    k3 = walk(odd, job(title="Odd sender"), jobs.ACCEPTED, jobs.RUNNING)
    assert odd.get(k3.id).held == "running" or odd.get(k3.id).stalled, odd.get(k3.id)
    return {"timeout_edit_retried": True, "not_modified": "success", "deleted_message": "held, retried on next status",
            "fresh_budget_per_status": True, "flood": "stalled, retried after restart", "retry_after_s": wait}


def restart_recovery():
    history, sender = MemoryHistory(), ScriptedSender()
    desk = desk_for(history, sender)
    j = walk(desk, job(title="Restart task"), jobs.ACCEPTED, jobs.RUNNING)
    stored = desk.get(j.id)
    history.set_meta(receipts.report_key(j.id), json.dumps(asdict(replace(stored, status="completed"))))
    crashed = replace(stored, job_id="jb_crashed_" + "c" * 12, state="sending", owner="1:0:dead",
                      message_id=None, shown=None)
    history.set_meta(receipts.report_key(crashed.job_id), json.dumps(asdict(crashed)))
    pending = replace(stored, job_id="jb_pending_" + "p" * 12, state="pending", message_id=None, shown=None,
                      title="Pending task", status="queued")
    history.set_meta(receipts.report_key(pending.job_id), json.dumps(asdict(pending)))
    legacy = {"job_id": "jb_legacy_" + "l" * 12, "scope": SCOPE, "destination": PRIVATE, "state": "pending",
              "text": "✅ Hermes task 9 finished: old\n\nold body", "attachment": "/x/old.md", "attempts": 0}
    history.set_meta(receipts.report_key(legacy["job_id"]), json.dumps(legacy))
    after = ScriptedSender()
    desk = desk_for(history, after)
    desk.reconcile()
    for job_id in (j.id, pending.job_id):
        settle(desk, job_id)
    deadline = time.time() + 5
    while desk.get(j.id).shown != "completed" and time.time() < deadline:
        time.sleep(0.01)
    assert desk.get(crashed.job_id).state == "uncertain", "a claim left by a dead process is not resent"
    assert desk.get(j.id).shown == "completed"
    assert sorted(c["text"] for c in after.calls) == ["✅ Restart task", "🕓 Pending task"], after.calls
    assert [c["kind"] for c in after.calls if c["text"] == "✅ Restart task"] == ["edit"]
    assert desk.get(legacy["job_id"]).state == "pending" and legacy["text"] not in [c["text"] for c in after.calls], \
        "a report stored by an earlier version is kept as is and never sent"
    again = ScriptedSender()
    desk = desk_for(history, again)
    desk.reconcile()
    time.sleep(0.3)
    assert not again.calls, "a second restart has nothing to send"
    return {"lagging_message": "edited once", "dead_claim": "uncertain", "pending": "sent once", "legacy": "untouched",
            "second_restart_calls": 0}


def binding_and_since():
    history, sender = MemoryHistory(), ScriptedSender()
    clock = Clock()
    desk = desk_for(history, sender, clock)
    old = job(created=clock.now - 10, title="Before reports")
    desk.follow(old)
    assert desk.get(old.id) is None, "a task accepted before reports existed gets no message"
    named = asdict(receipts.Destination("named", -1009876543210, 17, "ops", BOT))
    walk(desk, job(created=clock.now + 1, title="Named task", report_to=named), jobs.ACCEPTED, jobs.RUNNING)
    assert {c["chat_id"] for c in sender.calls} == {-1009876543210}, sender.calls
    assert sender.calls[0]["thread_id"] == 17 and not [c for c in sender.calls if c["chat_id"] == USER]
    legacy_job = replace(job(created=clock.now + 2, title="Unbound"), report_to=None)
    walk(desk, legacy_job, jobs.ACCEPTED)
    assert sender.calls[-1]["chat_id"] == USER, "a job without a bound destination goes to its own user's chat"
    return {"before_since": "no message", "named": "chat -1009876543210 topic 17 only",
            "unbound": "the scope's own private chat"}


class FakeBotApi:
    """A local stand-in for api.telegram.org: answers each request from `replies` (default: the sent message),
    records what it was asked."""

    def __init__(self):
        outer = self
        self.replies, self.seen = [], []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                outer.seen.append((self.path, body))
                reply = outer.replies.pop(0) if outer.replies else (200, {"ok": True, "result": {"message_id": 8001}})
                if reply == "hang":
                    time.sleep(3)
                    return
                raw = reply[1] if isinstance(reply[1], bytes) else json.dumps(reply[1]).encode()
                self.send_response(reply[0])
                for name, value in (reply[2] if len(reply) > 2 else {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                outer.seen.append((self.path, None))
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def refusal(status, description, **more):
    return status, {"ok": False, "error_code": status, "description": description, **more}


def telegram_sender_contract():
    """The real TelegramSender over real sockets against a local fake Bot API; nothing from Hermes is imported."""
    api, real_api = FakeBotApi(), receipts.BOT_API
    receipts.BOT_API = api.base
    os.environ.pop("TELEGRAM_PROXY", None)
    hermes = {name for name in sys.modules if name.split(".")[0] in ("tools", "gateway", "plugins", "telegram")}
    try:
        sender = receipts.TelegramSender(TOKEN, timeout=1.0)
        dest = receipts.Destination.private(USER, BOT)
        sent = sender.send(dest, "🕓 Check *build*")
        edited = sender.edit(dest, "8001", "✅ Check *build*")
        assert sent == edited == {"success": True, "message_id": "8001"}, (sent, edited)
        assert api.seen == [(f"/bot{TOKEN}/sendMessage", {"chat_id": USER, "text": "🕓 Check *build*"}),
                            (f"/bot{TOKEN}/editMessageText", {"chat_id": USER, "message_id": 8001,
                                                               "text": "✅ Check *build*"})], api.seen
        foreign = receipts.Destination.private(USER, "654321")
        refusals = (sender.send(foreign, "x"), sender.edit(foreign, "8001", "x"))
        assert len(api.seen) == 2 and all(receipts.classify(r)[0] == "failed" for r in refusals), refusals
        assert receipts.classify_edit(refusals[1]) == receipts.EDIT_FINAL

        def topic(thread_id, *replies):
            del api.seen[:]
            api.replies[:] = replies
            result = sender.send(receipts.Destination("named", -1009876543210, thread_id, "ops", BOT), "x")
            return result, [body.get("message_thread_id") for _, body in api.seen]
        assert topic(17)[1] == [17] and topic(1)[1] == [None], "a topic is addressed; General (1) is the chat itself"
        fallback = topic(17, refusal(400, "Bad Request: message thread not found"))
        assert fallback[0].get("success") and fallback[1] == [17, None], fallback

        def first_send(*reply):
            api.replies[:] = [reply if len(reply) > 1 else reply[0]]
            result = sender.send(dest, "x")
            return receipts.classify(result)[0], receipts.retry_after(result.get("error")), result
        flood = refusal(429, "Too Many Requests: retry after 7", parameters={"retry_after": 7})
        assert first_send(*flood)[:2] == (receipts.PENDING, 7.0)
        for description in ("Bad Request: chat not found", "Forbidden: bot was blocked by the user",
                            "Bad Request: message is too long"):
            assert first_send(*refusal(description.startswith("F") and 403 or 400, description))[0] == receipts.FAILED
        for lost in ((502, b"<html>Bad Gateway</html>"), (200, b"not json"), refusal(401, "Unauthorized"), "hang"):
            state, _, result = first_send(*lost) if lost != "hang" else first_send(lost)
            assert state == receipts.UNCERTAIN and TOKEN not in json.dumps(result), (lost, result)
        assert "timed out after 1s; it may still have been delivered" in result["error"], result

        def edit(*reply):
            api.replies[:] = [reply if len(reply) > 1 else reply[0]]
            return receipts.classify_edit(sender.edit(dest, "8001", "x"))
        assert edit(*refusal(400, "Bad Request: message is not modified: same content")) == receipts.EDIT_DONE
        assert edit(*refusal(400, "Bad Request: message to edit not found")) == receipts.EDIT_FINAL
        assert edit(*refusal(400, "Bad Request: message can't be edited")) == receipts.EDIT_FINAL
        assert edit(*flood) == edit(502, b"x") == edit("hang") == receipts.EDIT_RETRY

        os.environ["TELEGRAM_PROXY"] = "http://127.0.0.1:9"  # nothing listens there
        del api.seen[:]
        blocked = sender.send(dest, "x")
        del os.environ["TELEGRAM_PROXY"]
        assert not api.seen and receipts.classify(blocked)[0] == receipts.PENDING, "no request goes around the proxy"

        api.close()  # nothing listens now: a refused connection proves nothing was sent
        gone = sender.send(dest, "x")
        assert receipts.classify(gone)[0] == receipts.PENDING and TOKEN not in json.dumps(gone), gone
        assert receipts.classify_edit(sender.edit(dest, "8001", "x")) == receipts.EDIT_RETRY
        loaded = {name for name in sys.modules if name.split(".")[0] in ("tools", "gateway", "plugins", "telegram")}
        assert loaded == hermes, f"the sender imported Hermes or python-telegram-bot modules: {sorted(loaded - hermes)}"
    finally:
        receipts.BOT_API = real_api
        api.close()
    return {"send": "Bot API sendMessage, plain text, no media", "edit": "Bot API editMessageText, plain text",
            "foreign_bot": "refused before any call", "deleted_topic": "sent to the chat itself",
            "refused_connection": "retried", "telegram_proxy": "honored", "lost_reply_or_timeout": "uncertain, never resent", "hermes_imports": 0}


def outage_held_edits_recover():
    """Edits held because Hermes' own sender broke (core 471537ae3a, 2026-11-16) are retried after a restart;
    an edit Telegram itself refused stays held."""
    history, sender = MemoryHistory(), ScriptedSender()
    desk = desk_for(history, sender)
    j = walk(desk, job(title="Outage task"), jobs.ACCEPTED, jobs.RUNNING)
    stored = desk.get(j.id)
    outage = replace(stored, status="completed", held="completed", stalled=False,
                     error="Hermes Telegram sender is not installed (ImportError)")
    refused = replace(stored, job_id="jb_refused_" + "r" * 12, status="completed", held="completed", stalled=False,
                      error="Telegram editMessageText failed: Bad Request: message to edit not found")
    for report in (outage, refused):
        history.set_meta(receipts.report_key(report.job_id), json.dumps(asdict(report)))
    after = ScriptedSender()
    desk = desk_for(history, after)
    desk.reconcile()
    deadline = time.time() + 5
    while desk.get(j.id).shown != "completed" and time.time() < deadline:
        time.sleep(0.01)
    assert desk.get(j.id).shown == "completed", desk.get(j.id)
    assert [(c["kind"], c["text"]) for c in after.calls] == [("edit", "✅ Outage task")], after.calls
    assert desk.get(refused.job_id).held == "completed", "an edit Telegram refused stays held"
    return {"outage_hold": "edited after restart", "telegram_refusal": "still held"}


def connect_failures_are_not_sent():
    """urllib raises URLError only before Telegram has the whole request, so a non-timeout OSError there means
    nothing was sent. A redirect is never followed, and an oversized reply is not trusted."""
    import ssl
    import urllib.error
    sender, dest, real_open = receipts.TelegramSender(TOKEN, timeout=1.0), receipts.Destination.private(USER, BOT), \
        receipts._open
    outcomes = {}
    try:
        for name, reason in (("unreachable", OSError(101, "Network is unreachable")), ("tls_reset", ssl.SSLError("reset")),
                             ("refused", ConnectionRefusedError()), ("timeout", TimeoutError())):
            def fail(request, timeout, reason=reason):
                raise urllib.error.URLError(reason)
            receipts._open = fail
            outcomes[name] = receipts.classify(sender.send(dest, "x"))[0]
    finally:
        receipts._open = real_open
    assert outcomes == {"unreachable": receipts.PENDING, "tls_reset": receipts.PENDING, "refused": receipts.PENDING,
                        "timeout": receipts.UNCERTAIN}, outcomes
    api, real_api = FakeBotApi(), receipts.BOT_API
    receipts.BOT_API = api.base
    os.environ.pop("TELEGRAM_PROXY", None)
    try:
        api.replies[:] = [(302, b"", {"Location": api.base + "/elsewhere"})]
        moved = sender.send(dest, "x")
        assert not any(path == "/elsewhere" for path, _ in api.seen), api.seen
        assert receipts.classify(moved)[0] == receipts.UNCERTAIN and TOKEN not in json.dumps(moved), moved
        api.replies[:] = [(200, b'{"ok": true, "result": {"message_id": 1}, "pad": "' + b"x" * 70000 + b'"}')]
        assert receipts.classify(sender.send(dest, "x"))[0] == receipts.UNCERTAIN, "an oversized reply is not trusted"
    finally:
        receipts.BOT_API = real_api
        api.close()
    return {**outcomes, "redirect": "not followed", "oversized_reply": "uncertain"}


def no_detail_in_messages():
    history, sender = MemoryHistory(), ScriptedSender()
    desk = desk_for(history, sender)
    j = job(request="research synthetic rates\nwith a long body " + "x" * 3000, title="Research rates")
    j = walk(desk, j, jobs.ACCEPTED, jobs.RUNNING)
    walk(desk, replace(j, result="SYNTHETIC RESULT " * 200), jobs.SUCCEEDED)
    assert all(len(c["text"]) <= 64 for c in sender.calls), sender.calls
    assert not any("SYNTHETIC RESULT" in c["text"] or "x" * 20 in c["text"] for c in sender.calls)
    assert not {"attachment", "text"} & set(asdict(desk.get(j.id))), "only the title and status are stored"
    return {"max_text_chars": max(len(c["text"]) for c in sender.calls), "attachments": 0}


CHECKS = (titles, other_language_named_in_english, every_status_survives_naming, naming_survives_a_restart, one_message_lifecycle, coalesced_transitions, uncertain_create_never_resent,
          refused_create_retries_then_fails, edits_converge, restart_recovery, binding_and_since,
          telegram_sender_contract, outage_held_edits_recover, connect_failures_are_not_sent, no_detail_in_messages)


def main():
    failures, evidence = 0, {}
    for check in CHECKS:
        try:
            evidence[check.__name__] = check()
            print(f"PASS [reports] {check.__name__}", flush=True)
        except Exception as exc:
            failures += 1
            evidence[check.__name__] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"FAIL [reports] {check.__name__}: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
                  flush=True)
    print(json.dumps(evidence, indent=1, default=str, ensure_ascii=False))
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} report checks passed")
    print("TALK_REPORTS_" + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
