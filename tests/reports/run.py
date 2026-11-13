#!/usr/bin/env python3
"""Focused regression for Telegram task status messages (hermes_talk/receipts.py), offline and without Hermes.

  python3 tests/reports/run.py        prints TALK_REPORTS_OK

One message per task, created once and then edited: its text is a status mark and a short English title,
never the result. The real ReportDesk and TelegramSender run against an in-memory SessionDB stand-in, a
scripted sender, and a synthetic Hermes sender module (tools.send_message_tool / tools.send_message_senders)
that never contacts Telegram.
"""
import asyncio
import json
import sys
import threading
import time
import traceback
import types
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hermes_talk import jobs, receipts  # noqa: E402
from hermes_talk.history import MemoryHistory  # noqa: E402

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
                          "gone": "Telegram edit failed: Message to edit not found"}[outcome]}

    def kinds(self):
        return [c["kind"] for c in self.calls]


def job(number=1, request="check the synthetic build", title="", created=None, report_to=PRIVATE):
    return jobs.new_job(SCOPE, "conv_1", "vs_1", 1, f"del_{number}", number, request,
                        created if created is not None else 2_000_000.0, report_to, title)


def desk_for(history, sender, clock=None, **kw):
    return receipts.ReportDesk(history, sender, clock or Clock(), **{"attempts": 3, "backoff": (0.0, 0.0), **kw})


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
    assert take("TITLE: Read inbox\nпрочитай почту") == ("прочитай почту", "Read inbox")
    assert take("cancel task 3\nTITLE: Cancel task") == ("cancel task 3", "Cancel task")
    assert take("найди рейс\nTITLE: Найти рейс") == ("найди рейс", ""), "a non-English title is not used"
    assert take("x\nTITLE: " + "word " * 12) == ("x", ""), "more than a few words is not a title"
    assert take("x\nTITLE: Fix *bold* `code` [link]") == ("x", "Fix bold code link")
    assert take("no directive here") == ("no directive here", "")
    # Conduit VoiceBackgroundJobs.title(for:): collapse whitespace; over 60 characters, cut at the last
    # space past the midpoint, else hard, and end with an ellipsis.
    conduit = receipts.conduit_title
    assert conduit("  Check   the\nbuild ") == "Check the build"
    long = "Summarize the quarterly synthetic infrastructure cost report for the operations team"
    # The 60-character prefix ends exactly at "report"; Swift's lastIndex(of: " ") still cuts before it.
    assert conduit(long) == "Summarize the quarterly synthetic infrastructure cost…", conduit(long)
    assert conduit("Summarize the quarterly synthetic infrastructure costs and emails") == \
        "Summarize the quarterly synthetic infrastructure costs and…"
    assert conduit("x" * 70) == "x" * 60 + "…"
    fallback = receipts.job_title
    assert fallback(job(title="Check build"), str) == "Check build"
    assert fallback(job(request="check the synthetic build"), str) == "check the synthetic build"
    assert fallback(job(number=4, request="проверь сборку"), str) == "Hermes task 4", "no fake translation"
    secret = "sk-" + "s" * 24
    redact = lambda t: t.replace(secret, "[redacted]")
    assert secret not in fallback(job(request=f"rotate {secret}"), redact)
    return {"directive": "TITLE: <few English words>", "fallback": "Conduit title(for:) on an English request, "
            "else 'Hermes task N'", "max_chars": receipts.TITLE_CHARS}


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
    return {"timeout_edit_retried": True, "not_modified": "success", "deleted_message": "held, retried on next status"}


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


def install_synthetic_hermes(record, edit_error=None):
    """Synthetic tools.send_message_tool / tools.send_message_senders / gateway.config modules."""
    tools = types.ModuleType("tools")
    tool = types.ModuleType("tools.send_message_tool")
    senders = types.ModuleType("tools.send_message_senders")
    gateway = types.ModuleType("gateway")
    config = types.ModuleType("gateway.config")

    class Platform:
        TELEGRAM = "telegram"

    class PlatformConfig:
        def __init__(self, enabled, token):
            self.enabled, self.token = enabled, token

    async def send_to_platform(platform, pconfig, chat_id, message, thread_id=None, media_files=None,
                               force_document=False):
        record.append({"kind": "send", "token": pconfig.token, "chat_id": chat_id, "thread_id": thread_id,
                       "text": message, "media": media_files})
        return {"success": True, "message_id": "8001"}

    class Bot:
        def __init__(self, token):
            self.token = token

        async def edit_message_text(self, chat_id, message_id, text):
            record.append({"kind": "edit", "token": self.token, "chat_id": chat_id, "message_id": message_id,
                           "text": text})
            if edit_error == "hang":
                await asyncio.sleep(3600)
            if edit_error:
                raise RuntimeError(edit_error)

    tool._send_to_platform = send_to_platform
    senders._telegram_bot = Bot
    config.Platform, config.PlatformConfig = Platform, PlatformConfig
    sys.modules.update({"tools": tools, "tools.send_message_tool": tool, "tools.send_message_senders": senders,
                        "gateway": gateway, "gateway.config": config})


def telegram_sender_contract():
    record = []
    install_synthetic_hermes(record)
    sender = receipts.TelegramSender(TOKEN, timeout=1.0)
    dest = receipts.Destination.private(USER, BOT)
    sent = sender.send(dest, "🕓 Check build")
    edited = sender.edit(dest, "8001", "✅ Check build")
    assert sent == {"success": True, "message_id": "8001"} and edited["success"], (sent, edited)
    assert record[0]["media"] in (None, []) and record[0]["token"] == TOKEN and record[0]["chat_id"] == str(USER)
    assert record[1] == {"kind": "edit", "token": TOKEN, "chat_id": USER, "message_id": 8001,
                         "text": "✅ Check build"}, record[1]
    foreign = receipts.Destination.private(USER, "654321")
    refusals = (sender.send(foreign, "x"), sender.edit(foreign, "8001", "x"))
    assert len(record) == 2 and all(receipts.classify(r)[0] == "failed" for r in refusals), refusals
    assert receipts.classify_edit(refusals[1]) == receipts.EDIT_FINAL
    for error, expected in (("Message is not modified: same content", receipts.EDIT_DONE),
                            ("Message to edit not found", receipts.EDIT_FINAL),
                            ("Timed out", receipts.EDIT_RETRY)):
        install_synthetic_hermes(record, error)
        assert receipts.classify_edit(sender.edit(dest, "8001", "x")) == expected, error
    install_synthetic_hermes(record, "hang")
    started = time.monotonic()
    hung = sender.edit(dest, "8001", "x")
    assert receipts.classify_edit(hung) == receipts.EDIT_RETRY and time.monotonic() - started < 3, hung
    for name in ("tools", "tools.send_message_tool", "tools.send_message_senders", "gateway", "gateway.config"):
        sys.modules.pop(name, None)
    missing = sender.edit(dest, "8001", "x")
    assert "not installed" in missing["error"] and receipts.classify_edit(missing) == receipts.EDIT_FINAL
    return {"send": "Hermes _send_to_platform, no media", "edit": "Hermes _telegram_bot().edit_message_text",
            "foreign_bot": "refused before any call", "edit_timeout": "retry"}


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


CHECKS = (titles, one_message_lifecycle, coalesced_transitions, uncertain_create_never_resent,
          refused_create_retries_then_fails, edits_converge, restart_recovery, binding_and_since,
          telegram_sender_contract, no_detail_in_messages)


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
