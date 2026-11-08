#!/usr/bin/env python3
"""Pulse engine parity: model selection, memory extraction and follow-up checks through the product.

  python3 verify-pulse-engine.py --offline

re-runs itself under a real Hermes runtime (HERMES_TALK_HERMES_PYTHON, else the first
~/.hermes/installs/*/environments/*/venv/bin/python) with a disposable HERMES_HOME under TMPDIR
that installs and enables this plugin and names a local deterministic model
(tests/pulse/model_fixture.py) as the profile's model. The engine reaches that model only
through the real route: Hermes' plugin loader runs hermes-telegram-talk's register(ctx), which binds
ctx.llm (agent.plugin_llm.PluginLlm), and every Pulse call goes through the host's call_llm over
HTTP. Calls, events, feedback and call ends go through the real /tg-voice/ routes with signed
synthetic initData; storage is Hermes' own SessionDB. Synthetic text only; no paid or live
provider. A missing runtime FAILS (this gate has no skip). Prints TALK_PULSE_ENGINE_PARITY_OK
when every step passed.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "pulse"))
_spec = importlib.util.spec_from_file_location("verify_backend", ROOT / "verify-backend.py")
vb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vb)
fx = vb.fx
import server  # noqa: E402
from hermes_talk import pulse, pulse_followup, pulse_model, pulse_select  # noqa: E402
from hermes_talk.config import parse_config  # noqa: E402
from hermes_talk.talk import TalkRuntime  # noqa: E402

PLUGIN_FILES = ("plugin.yaml", "__init__.py", "server.py", "hermes_talk", "LICENSES")
A, B = fx.USER_ID, fx.OTHER_USER_ID
STEPS = []


def step(fn):
    STEPS.append(fn)
    return fn


def brief(instructions):
    if "## Activity since the last visit\n" not in instructions:
        return ""
    return instructions.split("## Activity since the last visit\n", 1)[1].split("\n\n## ", 1)[0]


def lines(instructions):
    return re.findall(r"^- (P\d+) \[(\w+)\] (.+?) \((?:\w+ session|Pulse memory item) ", brief(instructions), re.M)


class World(vb.Harness):
    """The real server and runtime on one disposable home, reopenable to prove restart behavior."""

    def __init__(self, home, clock, model):
        self.native, self.home, self.clock, self.model = True, home, clock, model
        self.fake = fx.FakeUpstream()
        self.config = parse_config(fx.config_data(context={"skills": [], "budgets": {"activity_items": 2}}), home)
        self.seq = 0
        self.open()

    def open(self, pulse_model_override=None):
        from hermes_talk.history import NativeHistory
        self.history = NativeHistory(self.home)
        kwargs = {"model": pulse_model_override} if pulse_model_override is not None else {}
        self.talk = TalkRuntime(self.config, fx.BOT_TOKEN, self.history, clock=self.clock, **kwargs)
        self.app = server.VoiceApp(fx.BOT_TOKEN, {A, B}, fx.ORIGIN, "/tg-voice/", resolve=self.fake.resolve,
                                   post=self.fake.post, clock=self.clock, talk=self.talk)
        self.httpd = server.BoundedServer(("127.0.0.1", 0), server.make_handler(self.app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.history.close()

    def reopen(self, pulse_model_override=None):
        self.close()
        self.open(pulse_model_override)

    def start(self, user=A, conversation_id=None):
        data, instructions = self.call(conversation_id, user=user)
        return data["conversation_id"], data["voice_session_id"], instructions

    def say(self, conv, vs, *turns, user=A):
        events = []
        for role, text in turns:
            events.append({"id": f"e{self.seq}", "sequence": self.seq, "role": role, "text": text})
            self.seq += 1
        return self.ok("events", {"conversation_id": conv, "voice_session_id": vs, "events": events}, user)

    def end(self, conv, vs, user=A):
        return self.ok("end", {"conversation_id": conv, "voice_session_id": vs}, user)

    def state(self, user=A):
        return self.talk.pulse.snapshot(self.talk.account(user))

    def memory(self, user=A):
        return list(self.state(user)["memory"]["items"].values())

    def item(self, kind, user=A, contains=""):
        found = [i for i in self.memory(user) if i["kind"] == kind and contains in i["evidence"]]
        assert len(found) == 1, (kind, contains, [(i["kind"], i["evidence"]) for i in self.memory(user)])
        return found[0]

    def advance(self, seconds):
        self.clock.now += seconds


@step
def the_engine_reaches_the_model_only_through_the_host_plugin_llm(w):
    llm = pulse_model.bound()
    assert type(llm).__module__ == "agent.plugin_llm" and type(llm).__name__ == "PluginLlm", type(llm)
    assert getattr(llm, "_plugin_id", None) == "hermes-telegram-talk", llm._plugin_id
    assert w.talk.engine.model.available() and w.talk.engine.model.label() == "plugin_llm:hermes-telegram-talk"
    return {"route": f"{type(llm).__module__}.{type(llm).__name__}", "plugin_id": llm._plugin_id}


@step
def the_model_selects_the_briefing_not_the_status_ranking(w):
    w.seed("Synthetic broken backup", turns=[("user", "run"), ("assistant", "boom")], failed=True, age_s=900)
    w.seed("Synthetic invoice draft", turns=[("user", "go"), ("assistant", "drafted")], end_reason="done", age_s=800)
    w.seed("Synthetic routine sync", turns=[("user", "go"), ("assistant", "synced")], end_reason="done", age_s=700)
    w.seed("Synthetic deploy check", turns=[("user", "go"), ("assistant", "deployed")], end_reason="done", age_s=600)
    before = len(w.model.requests)
    conv, vs, instructions = w.start()
    shown = lines(instructions)
    titles = [t for _, _, t in shown]
    assert titles == ["Synthetic invoice draft", "Synthetic deploy check"], shown
    assert "fixture-model judged 'Synthetic invoice draft'" in brief(instructions), brief(instructions)
    state = w.state()
    assert state["selection"]["by"] == "model" and state["selection"]["model"] == "plugin_llm:hermes-telegram-talk"
    decisions = {state["items"][fp]["title"]: d for fp, d in state["selection"]["decisions"].items()}
    assert decisions["Synthetic routine sync"] == {"decision": "skipped", "skip": "minor",
                                                   "why": "fixture-model judged 'Synthetic routine sync'"}
    assert decisions["Synthetic broken backup"]["decision"] == "over_limit", decisions
    ranked, _ = pulse.select([dict(c) for c in state["items"].values()], state["memory"], w.clock(), 2)
    assert ranked[0]["title"] == "Synthetic broken backup", "the deterministic ranking would differ"
    sent = [m for k, m, fmt in w.model.requests[before:] if k == "selection"]
    assert sent and fmt_ok(w.model.requests[before:]), "selection went over HTTP as a JSON-mode call"
    w.say(conv, vs, ("assistant", "Your Synthetic invoice draft is ready."), ("user", "great"))
    cards = {c["title"]: c for c in state_cards(w)}
    assert cards["Synthetic invoice draft"]["state"] == "acknowledged", cards["Synthetic invoice draft"]
    assert cards["Synthetic deploy check"]["state"] == "exposed"
    assert cards["Synthetic routine sync"]["state"] == "candidate", "a skipped card is never exposed"
    w.end(conv, vs)
    w.ctx = {"conv": conv}
    return {"briefing": titles, "deterministic_first": ranked[0]["title"],
            "decisions": {t: d["decision"] for t, d in decisions.items()}}


def fmt_ok(requests):
    return all((fmt or {}).get("type") == "json_object" for k, _, fmt in requests if k != "other")


def state_cards(w, user=A):
    return list(w.state(user)["items"].values())


@step
def memory_is_extracted_by_the_model_with_quoted_provenance(w):
    w.lease_day = datetime.fromtimestamp(w.clock() + 2 * 86400, timezone.utc).strftime("%Y-%m-%d")
    conv, vs, _ = w.start(conversation_id=w.ctx["conv"])
    w.say(conv, vs,
          ("user", "I'll renew the synthetic parking permit. I'm thinking about learning the cello."),
          ("assistant", "I'll check the synthetic build logs and get back to you."),
          ("user", "Also tell me when the synthetic rocket launch happens."),
          ("user", f"And I will send the lease draft to Mira by {w.lease_day}."),
          ("user", "FABRICATE one for the record."))
    w.end(conv, vs)
    summary = w.talk.engine.run_once(A)
    mem = summary["memory"]
    assert mem["by"] == "model" and mem["rejected"] >= 2, mem
    commitment = w.item("commitment", contains="parking permit")
    promise = w.item("promise", contains="build logs")
    watch = w.item("watch", contains="rocket")
    interest = w.item("interest", contains="cello")
    dated = w.item("commitment", contains="lease draft")
    assert dated["due"] == w.lease_day and dated["due_at"] is not None, dated
    assert commitment["due"] == "" and commitment["due_at"] is None
    for item in (commitment, promise, watch, interest, dated):
        ref = item["provenance"][0]
        assert ref["session"] == conv and ref["source"] == "talk" and ref["evidence"] == item["evidence"], ref
        assert any(item["evidence"] in text for _, text in w.stored(conv)), item["evidence"]
    assert not [i for i in w.memory() if "yacht" in i["text"] or i["text"] == "Misattributed."]
    assert promise["next_check"] == promise["recorded"] + pulse_followup.FIRST_CHECK_S
    assert commitment["next_check"] == commitment["recorded"] + pulse_followup.COMMITMENT_FIRST_S
    again = w.talk.engine.run_once(A)["memory"]
    assert again["read"] == 0 and again["kept"] == 0, "lines already read are not read again"
    return {"kinds": sorted(i["kind"] for i in w.memory()), "rejected": mem["rejected"],
            "provenance": promise["provenance"][0]}


@step
def followups_wait_back_off_then_tell_through_a_card(w):
    w.advance(pulse_followup.FIRST_CHECK_S + 1)
    first = w.talk.engine.run_once(A)["followups"]
    promise = w.item("promise")
    assert first["checked"] >= 1 and promise["checks"] == 1 and promise["log"][-1]["decision"] == "wait", promise
    assert promise["next_check"] == w.clock() + 3600.0, "the model's one-hour hint shortens the 2 h backoff"
    w.advance(1800)
    assert w.talk.engine.run_once(A)["followups"]["checked"] == 0, "not checked again before its backoff"
    w.advance(1801)
    told = w.talk.engine.run_once(A)["followups"]
    promise = w.item("promise")
    assert told["told"] == 1 and promise["card"], (told, promise)
    card = w.state()["items"][promise["card"]]
    assert card["kind"] == "followup" and card["title"].startswith("Follow-up: Hermes said: I'll check"), card
    conv, vs, instructions = w.start(conversation_id=w.ctx["conv"])
    shown = lines(instructions)
    assert shown[0][1] == "followup" and shown[0][2] == card["title"], shown
    w.say(conv, vs, ("assistant", "Hello there."), ("user", "hi"))
    assert w.state()["items"][promise["card"]]["state"] == "exposed", "a greeting alone does not deliver it"
    w.say(conv, vs, ("assistant", card["title"]), ("user", "thanks, noted"))
    after = w.item("promise")
    assert w.state()["items"][promise["card"]]["state"] == "acknowledged" and after["reminded"] == w.clock()
    assert after["card"] is None and after["state"] == "open", after
    w.end(conv, vs)
    run = w.talk.engine.run_once(A)["memory"]
    promises = [i for i in w.memory() if i["kind"] == "promise"]
    assert len(promises) == 1 and run["rejected"] >= 1, ("Pulse's own spoken reminder is never a new item", promises)
    return {"checks": after["checks"], "card": card["title"],
            "log": [e.get("decision", "told") for e in after["log"]], "reminder_line_rejected": run["rejected"]}


@step
def a_followup_check_resolves_and_drops_items_from_new_material(w):
    conv, vs, _ = w.start(conversation_id=w.ctx["conv"])
    w.say(conv, vs, ("user", "About the parking permit: I already did it."),
          ("user", "And the lease draft, never mind, Mira has it."))
    w.end(conv, vs)
    w.advance(pulse_followup.COMMITMENT_FIRST_S)
    w.talk.engine.run_once(A)
    permit = w.item("commitment", contains="parking permit")
    assert permit["state"] == "done" and permit["outcome"]["by"] == "follow-up check", permit
    lease = w.item("commitment", contains="lease draft")
    assert lease["state"] == "open" and lease["checks"] == 0, "a dated commitment waits for its day"
    w.clock.now = lease["next_check"] + 1
    w.talk.engine.run_once(A)
    lease = w.item("commitment", contains="lease draft")
    assert lease["state"] == "cancelled" and lease["outcome"]["decision"] == "drop", lease
    return {"permit": permit["outcome"], "lease": lease["outcome"]}


@step
def a_failed_check_retries_without_counting_and_a_watch_gives_up(w):
    watch = w.item("watch")
    w.clock.now = max(w.clock(), watch["next_check"]) + 1
    w.model.fail = True
    try:
        w.talk.engine.run_once(A)
    finally:
        w.model.fail = False
    failed = w.item("watch")
    assert failed["checks"] == watch["checks"] and failed["failures"] == 1, failed
    assert failed["next_check"] == w.clock() + pulse_followup.RETRY_S
    runs = 0
    while w.item("watch")["state"] == "open" and runs < 40:
        w.clock.now = w.item("watch")["next_check"] + 1
        w.talk.engine.run_once(A)
        runs += 1
    final = w.item("watch")
    assert final["state"] == "gave_up" and final["checks"] == pulse_followup.MAX_CHECKS, final
    assert all(e["decision"] == "wait" for e in final["log"]), final["log"]
    return {"checks": final["checks"], "runs": runs, "outcome": final["outcome"]}


@step
def feedback_teaches_the_model_selection_and_dismisses_a_followup(w):
    w.seed("Synthetic invoice nightly report", turns=[("user", "go"), ("assistant", "reported")], end_reason="done",
           age_s=60)
    conv, vs, instructions = w.start(conversation_id=w.ctx["conv"])
    handle = {t: h for h, _, t in lines(instructions)}
    assert "Synthetic invoice nightly report" in handle, lines(instructions)
    status, data = w.post("pulse/feedback", {"initData": w.init(), "conversation_id": conv, "voice_session_id": vs,
                                             "delegation_item_id": "call_fb1",
                                             "request": f"pulse {handle['Synthetic invoice nightly report']} not useful"})
    assert status == 200 and data["card"]["feedback"] == "not_useful", data
    w.end(conv, vs)
    w.seed("Synthetic invoice nightly report!", source="cron", turns=[("user", "go"), ("assistant", "again")], age_s=0)
    w.talk.engine.run_once(A)
    sent = [m for k, m, _ in w.model.requests if k == "selection"][-1]
    assert 'were not useful: "Synthetic invoice nightly report"' in sent[-1]["content"][0]["text"]
    state = w.state()
    rated = [d for fp, d in state["selection"]["decisions"].items()
             if state["items"][fp]["title"] == "Synthetic invoice nightly report!"
             and state["items"][fp]["state"] == "candidate"]
    assert rated and rated[0]["decision"] == "skipped" and rated[0]["skip"] == "rated", rated
    return {"lesson_sent": True, "decision": rated[0]}


@step
def accounts_never_share_memory_followups_or_selection(w):
    conv, vs, instructions = w.start(user=B)
    assert "Follow-up:" not in instructions and "cello" not in instructions
    w.say(conv, vs, ("user", "I'll water the synthetic ferns."), user=B)
    w.end(conv, vs, user=B)
    w.talk.engine.run_once(B)
    mine = [i["evidence"] for i in w.memory(B)]
    assert mine == ["I'll water the synthetic ferns."], mine
    assert not [i for i in w.memory(A) if "ferns" in i["evidence"]]
    assert all(c.get("kind") != "followup" for c in state_cards(w, B))
    return {"other_account_memory": mine}


@step
def dismissing_a_followup_card_cancels_its_item(w):
    w.advance(pulse_followup.COMMITMENT_FIRST_S + 1)
    w.talk.engine.run_once(B)
    w.advance(3601)
    assert w.talk.engine.run_once(B)["followups"]["told"] == 1
    conv, vs, instructions = w.start(user=B)
    shown = lines(instructions)
    assert shown and shown[0][1] == "followup" and "ferns" in shown[0][2], shown
    for _ in range(2):  # a resubmitted delegation changes nothing
        status, data = w.post("pulse/feedback", {"initData": w.init(B), "conversation_id": conv,
                                                 "voice_session_id": vs, "delegation_item_id": "call_fb2",
                                                 "request": f"pulse {shown[0][0]} dismiss"})
        assert status == 200 and data["card"] == {"handle": shown[0][0], "state": "dismissed", "feedback": "dismiss"}
    ferns = w.item("commitment", user=B)
    assert ferns["state"] == "cancelled" and ferns["outcome"]["by"] == "feedback" and ferns["card"] is None, ferns
    status, _ = w.post("pulse/feedback", {"initData": w.init(A), "conversation_id": conv, "voice_session_id": vs,
                                          "delegation_item_id": "call_fb3", "request": f"pulse {shown[0][0]} useful"})
    assert status in (403, 404), "another account cannot give feedback on this call's cards"
    w.end(conv, vs, user=B)
    return {"outcome": ferns["outcome"]}


@step
def restart_keeps_memory_schedule_selection_and_read_marks(w):
    before = {i["id"]: (i["state"], i["checks"], i["next_check"]) for i in w.memory()}
    selection = w.state()["selection"]
    w.reopen()
    after = {i["id"]: (i["state"], i["checks"], i["next_check"]) for i in w.memory()}
    assert after == before and w.state()["selection"] == selection
    assert w.talk.engine.run_once(A)["memory"]["kept"] == 0, "a restart does not reread or duplicate memory"
    engine = w.talk.engine
    assert engine.known_users() == {A, B}, "a restarted worker resumes every account with a Pulse record"
    # The worker server.py starts (there after START_DELAY_S): it loads the route through the same loader, then
    # resumes both accounts without waiting for a call to end.
    engine.start(loader=pulse_model.load_host, delay=0)
    deadline = time.time() + 60
    while not {A, B} <= set(engine.last) and time.time() < deadline:
        time.sleep(0.1)
    engine.stop()
    engine.thread.join(60)
    assert {A, B} <= set(engine.last) and not engine.thread.is_alive(), engine.last
    return {"items": len(after), "resumed_users": sorted(engine.known_users()), "worker_ran": sorted(engine.last)}


@step
def interests_decay_unless_said_again(w):
    interest = w.item("interest")
    assert interest["state"] == "open"
    w.advance(120 * 86400)
    w.talk.engine.run_once(A)
    faded = w.item("interest")
    assert faded["state"] == "faded" and faded["outcome"]["by"] == "decay", faded
    return {"after_days": 120, "state": faded["state"]}


def seed_rows(w, title, rows, source="telegram", age_s=0):
    """A synthetic session whose rows carry tool calls and tool results, through public SessionDB methods."""
    from hermes_state_ids import new_session_id
    w.advance(5)  # a later second than the scan cursor: the frozen test clock never repeats a session's time
    db, sid, at = w.history.db, new_session_id(), w.clock() - age_s
    db.create_session(sid, source)
    db.set_session_title(sid, title)
    for row in rows:
        db.append_message(sid, row["role"], content=row.get("content"), tool_name=row.get("tool_name"),
                          tool_calls=row.get("tool_calls"), tool_call_id=row.get("tool_call_id"), timestamp=at)
    db.end_session(sid, "done")
    return sid


def fresh_seed(w, *args, **kwargs):
    w.advance(5)  # as seed_rows: each seeded session is newer than the last scan
    return w.seed(*args, **kwargs)


def tool_call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def card_by_title(w, title, user=A):
    found = [c for c in state_cards(w, user) if c["title"] == title]
    assert len(found) == 1, (title, [c["title"] for c in state_cards(w, user)])
    return found[0]


def call_handles(w, user=A):
    conv, vs, instructions = w.start(user=user, conversation_id=w.ctx.get("conv") if user == A else None)
    return conv, vs, instructions, {t: h for h, _, t in lines(instructions)}


def rate(w, conv, vs, handle, words, delegation, user=A):
    return w.post("pulse/feedback", {"initData": w.init(user), "conversation_id": conv, "voice_session_id": vs,
                                     "delegation_item_id": delegation, "request": f"pulse {handle} {words}"})


def with_config(w, **changes):
    import dataclasses
    w.talk.config = dataclasses.replace(w.config, **changes)


@step
def claims_are_judged_against_their_turns_tool_calls(w):
    backed = seed_rows(w, "Synthetic dentist reminder", [
        {"role": "user", "content": "Please remind me to call the synthetic dentist at 5pm."},
        {"role": "assistant", "content": "", "tool_calls": [tool_call("call_r1", "cronjob", {"action": "create"})]},
        {"role": "tool", "tool_name": "cronjob", "tool_call_id": "call_r1", "content": '{"success": true}'},
        {"role": "assistant", "content": "Done, I set a reminder for 5pm to call the synthetic dentist."}])
    seed_rows(w, "Synthetic late text", [
        {"role": "user", "content": "Text synthetic Sam that I am late."},
        {"role": "assistant", "content": "Sure, I sent the message to synthetic Sam."}])
    seed_rows(w, "Synthetic landlord email", [
        {"role": "user", "content": "Email the synthetic landlord about the boiler."},
        {"role": "assistant", "content": "", "tool_calls": [tool_call("call_m1", "send_message", {})]},
        {"role": "tool", "tool_name": "send_message", "tool_call_id": "call_m1", "content": '{"error": "denied"}'},
        {"role": "assistant", "content": "I sent the message to the synthetic landlord."}])
    summary = w.talk.engine.run_once(A)["memory"]
    claims = {c["evidence"]: c for c in w.state()["memory"]["claims"]}
    dentist = claims["I set a reminder for 5pm to call the synthetic dentist."]
    sam = claims["I sent the message to synthetic Sam."]
    landlord = claims["I sent the message to the synthetic landlord."]
    assert dentist["backed_by"] == "cronjob create -> ok" and dentist["session"] == backed, dentist
    assert sam["backed_by"] == "" and sam["calls"] == [], sam
    assert landlord["backed_by"] == "" and landlord["calls"] == ["send_message -> error: denied"], landlord
    cards = [c for c in state_cards(w) if c.get("kind") == "claim"]
    assert sorted(c["evidence"] for c in cards) == sorted([sam["evidence"], landlord["evidence"]]), cards
    assert "card" not in dentist, "a backed claim never becomes a card"
    conv, vs, instructions = w.start(conversation_id=w.ctx["conv"])
    w.end(conv, vs)
    levels = {c["evidence"]: (c.get("delivery") or {}).get("level") for c in state_cards(w) if c.get("kind") == "claim"}
    assert set(levels.values()) <= {"screen", "off"} and "push" not in levels.values(), levels
    sent = [m for k, m, _ in w.model.requests if k == "selection"][-1][-1]["content"]
    text = "\n".join(p.get("text", "") for p in sent) if isinstance(sent, list) else sent
    assert "[claim with no tool call behind it" in text and "send_message -> error: denied" in text
    w.reopen()
    again = w.talk.engine.run_once(A)["memory"]
    assert len(w.state()["memory"]["claims"]) == 3 and again.get("claims", 0) == 0, "a restart rereads nothing"
    assert not any(c.get("kind") == "claim" for c in state_cards(w, B)), "claims are kept per account"
    return {"claims": summary.get("claims"), "unbacked": summary.get("unbacked"), "levels": levels,
            "backed_by": dentist["backed_by"], "landlord_calls": landlord["calls"]}


@step
def learned_preferences_turn_off_a_place_the_user_rates_down(w):
    with_config(w, budgets=dataclasses_replace(w.config.budgets, activity_items=8))
    try:
        fresh_seed(w, "Synthetic kanban board tidy", source="kanban", turns=[("user", "go"), ("assistant", "tidied")],
               end_reason="done", age_s=0)
        w.talk.engine.run_once(A)
        conv, vs, instructions, handle = call_handles(w)
        assert "Synthetic kanban board tidy" in handle, lines(instructions)
        before = card_by_title(w, "Synthetic kanban board tidy")["delivery"]
        assert before["level"] in ("push", "screen"), before
        status, data = rate(w, conv, vs, handle["Synthetic kanban board tidy"], "not useful", "call_lp1")
        assert status == 200 and data["card"]["feedback"] == "not_useful", data
        w.end(conv, vs)
        signals = w.state()["memory"]["signals"]
        assert [s["place"] for s in signals.values() if s["useful"] == 0.0 and s["place"] == "source:kanban"]
        fresh_seed(w, "Synthetic kanban column sweep", source="kanban", turns=[("user", "go"), ("assistant", "swept")],
               end_reason="done", age_s=0)
        w.talk.engine.run_once(A)
        decision = w.state()["selection"]["decisions"][card_by_title(w, "Synthetic kanban column sweep")["fp"]]
        assert decision["decision"] in ("surfaced", "over_limit"), "the model still picks it"
        conv, vs, instructions, handle = call_handles(w)
        w.end(conv, vs)
        after = card_by_title(w, "Synthetic kanban column sweep")
        assert "Synthetic kanban column sweep" not in handle and after["state"] == "candidate", lines(instructions)
        assert after["delivery"]["level"] == "off" and "kanban" in after["delivery"]["learned"], after["delivery"]
        w.reopen()
        with_config(w, budgets=dataclasses_replace(w.config.budgets, activity_items=8))
        conv, vs, instructions, handle = call_handles(w)
        w.end(conv, vs)
        assert "Synthetic kanban column sweep" not in handle, "what learning holds survives a restart"
        other = w.state(B)["memory"]
        theirs = [x["place"] for x in (other.get("signals") or {}).values()]
        assert "source:kanban" not in theirs, ("another account's ratings teach nothing there", theirs)
        return {"before": before["level"], "after": after["delivery"], "other_account_places": theirs}
    finally:
        w.talk.config = w.config


def dataclasses_replace(obj, **changes):
    import dataclasses
    return dataclasses.replace(obj, **changes)


@step
def delivery_policy_holds_for_the_night_and_quiet_then_briefs_once(w):
    from hermes_talk.config import PulseSettings
    from datetime import datetime, timezone
    hour = datetime.fromtimestamp(w.clock(), timezone.utc).hour
    night = ((hour - 1) % 24, 0), ((hour + 2) % 24, 0)
    budgets = dataclasses_replace(w.config.budgets, activity_items=8)
    with_config(w, budgets=budgets, pulse=PulseSettings(night=night))
    try:
        fresh_seed(w, "Synthetic lease renewal draft", source="desktop", turns=[("user", "go"), ("assistant", "drafted")],
               end_reason="done", age_s=0)
        w.talk.engine.run_once(A)
        conv, vs, instructions, handle = call_handles(w)
        w.end(conv, vs)
        held = card_by_title(w, "Synthetic lease renewal draft")
        assert "Synthetic lease renewal draft" not in handle and held["state"] == "candidate", lines(instructions)
        assert held["delivery"]["level"] == "brief" and held["delivery"]["why"] == "held: night", held["delivery"]
        w.advance(3 * 3600)
        w.reopen()
        with_config(w, budgets=budgets, pulse=PulseSettings(night=night))
        conv, vs, instructions, handle = call_handles(w)
        w.end(conv, vs)
        assert "Synthetic lease renewal draft" in handle, lines(instructions)
        assert "held for the brief" in [l for l in brief(instructions).splitlines()
                                         if "Synthetic lease renewal draft" in l][0]
        # Supplied to the model is not heard: a brief call that ends unanswered consumes neither the day's brief
        # nor the card, so a call a minute later (and after a restart) carries it again.
        unheard = card_by_title(w, "Synthetic lease renewal draft")
        assert not w.state().get("brief") and not unheard.get("briefed") and unheard["state"] == "exposed", (
            w.state().get("brief"), unheard)
        w.advance(60)
        w.reopen()
        with_config(w, budgets=budgets, pulse=PulseSettings(night=night))
        conv, vs, instructions, handle = call_handles(w)
        assert "Synthetic lease renewal draft" in handle, ("unheard brief debt stays owed", lines(instructions))
        assert not w.state().get("brief"), "being in the instructions again is still not hearing"
        w.say(conv, vs, ("assistant", "Your Synthetic lease renewal draft is ready."), ("user", "thanks"))
        w.end(conv, vs)
        stamp = w.state()["brief"]
        briefed = card_by_title(w, "Synthetic lease renewal draft")
        assert briefed["fp"] in stamp["cards"] and briefed["briefed"] == stamp["at"], (stamp, briefed)
        assert briefed["state"] == "acknowledged", briefed
        conv, vs, instructions, handle = call_handles(w)
        w.end(conv, vs)
        assert w.state()["brief"] == stamp, "the brief is once per local day"
        assert "Synthetic lease renewal draft" not in handle, "an acknowledged brief card is not carried again"
        until = w.clock() + 3600
        with_config(w, budgets=budgets, pulse=PulseSettings(night=night, quiet_until=until))
        fresh_seed(w, "Synthetic lease signing", source="desktop", turns=[("user", "go"), ("assistant", "signed")],
               end_reason="done", age_s=0)
        w.talk.engine.run_once(A)
        conv, vs, instructions, handle = call_handles(w)
        w.end(conv, vs)
        quiet = card_by_title(w, "Synthetic lease signing")
        assert "Synthetic lease signing" not in handle and quiet["delivery"]["why"] == "held: quiet until", quiet
        assert all((c.get("delivery") or {}).get("level") != "push" for c in state_cards(w)
                   if c["state"] in ("candidate", "exposed", "mentioned")) or not handle, "quiet: nothing leads"
        w.advance(86400)
        conv, vs, instructions, handle = call_handles(w)
        assert "Synthetic lease signing" in handle and w.state()["brief"] == stamp, lines(instructions)
        w.say(conv, vs, ("assistant", "Your Synthetic lease signing is done."), ("user", "good"))
        w.end(conv, vs)
        assert w.state()["brief"]["day"] != stamp["day"], "the next day's brief, once heard"
        return {"night": [list(night[0]), list(night[1])], "held": held["delivery"], "brief": stamp["day"],
                "quiet": quiet["delivery"]["why"]}
    finally:
        w.talk.config = w.config


@step
def rating_reasons_leave_lessons_that_change_delivery(w):
    budgets = dataclasses_replace(w.config.budgets, activity_items=8)
    with_config(w, budgets=budgets)
    try:
        for title in ("Synthetic discord lease digest", "Synthetic telegram lease digest", "Synthetic cron lease audit"):
            fresh_seed(w, title, source=title.split()[1], turns=[("user", "go"), ("assistant", "done")], end_reason="done")
        w.talk.engine.run_once(A)
        conv, vs, instructions, handle = call_handles(w)
        for title in ("Synthetic discord lease digest", "Synthetic telegram lease digest", "Synthetic cron lease audit"):
            assert title in handle, (title, lines(instructions))
        status, data = rate(w, conv, vs, handle["Synthetic discord lease digest"], "not useful because not relevant", "r1")
        assert status == 200, data
        assert rate(w, conv, vs, handle["Synthetic discord lease digest"], "not useful because not relevant", "r1")[0] == 200
        assert rate(w, conv, vs, handle["Synthetic telegram lease digest"], "not useful, already knew", "r2")[0] == 200
        assert rate(w, conv, vs, handle["Synthetic cron lease audit"], "not useful because too often", "r3")[0] == 200
        bad = rate(w, conv, vs, handle["Synthetic cron lease audit"], "not useful because meh", "r4")
        assert bad[0] == 400, bad
        w.end(conv, vs)
        lessons = w.state()["memory"]["lessons"]
        whats = [(l["what"], l.get("value")) for l in lessons]
        assert whats.count(("rule", "source:discord")) == 1, "a repeated delegation leaves one lesson"
        assert ("seen", None) in whats and ("level", "screen") in whats, whats
        fresh_seed(w, "Synthetic discord lease recap", source="discord", turns=[("user", "go"), ("assistant", "recap")],
               end_reason="done")
        w.talk.engine.run_once(A)
        sent = [m for k, m, _ in w.model.requests if k == "selection"][-1][-1]["content"]
        text = "\n".join(p.get("text", "") for p in sent) if isinstance(sent, list) else sent
        assert 'already knew about updates like these: "Synthetic telegram lease digest"' in text
        conv, vs, instructions, handle = call_handles(w)
        w.end(conv, vs)
        recap = card_by_title(w, "Synthetic discord lease recap")
        assert "Synthetic discord lease recap" not in handle and recap["delivery"]["why"] == "not relevant rule"
        loud = [(c["title"], c["delivery"]) for c in state_cards(w) if c.get("kind", "activity") == "activity"
                and (c.get("delivery") or {}).get("level") == "push" and c["state"] in ("candidate", "exposed")]
        assert not loud, ("too often: activity comes up no louder than screen", loud)
        assert not w.state(B)["memory"].get("lessons"), "lessons are per account"
        return {"lessons": whats}
    finally:
        w.talk.config = w.config
        w.advance(60)  # later work happens later: no session shares this frozen second with the ones seeded here


@step
def without_a_model_route_the_engine_degrades_and_says_so(w):
    pulse_model.bind(None)  # as in a Hermes whose plugin loader gave hermes-telegram-talk no ctx.llm
    w.reopen()
    try:
        w.seed("Synthetic offline note", turns=[("user", "go"), ("assistant", "noted")], end_reason="done", age_s=0)
        conv, vs, instructions = w.start(conversation_id=w.ctx["conv"])
        state = w.state()
        assert state["selection"]["by"] == "fallback" and state["selection"]["reason"] == "no model route", state[
            "selection"]
        assert "why now: fixture-model" not in brief(instructions), "a fallback never carries model reasons"
        w.end(conv, vs)
        summary = w.talk.engine.run_once(A)
        assert summary["memory"]["by"] == "unavailable" and summary["selection"] == "fallback", summary
    finally:
        pulse_model.bind(w.llm)
    return {"selection": "fallback", "memory": "unavailable"}


def run_host():
    tmp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    assert "/.hermes/profiles/" not in str(tmp.resolve()) + "/", "TMPDIR must not be under ~/.hermes/profiles/"
    from model_fixture import PulseFixtureModel
    model = PulseFixtureModel()
    home = fx.write_home(Path(tempfile.mkdtemp(prefix="talk-pulse-engine-", dir=tmp)))
    (home / "config.yaml").write_text(
        f"model:\n  default: scripted-pulse-model\n  provider: custom\n  base_url: {model.base_url}\n"
        "  api_key: synthetic-local-key\nplugins:\n  enabled:\n    - hermes-telegram-talk\n")
    target = home / "plugins" / "hermes-telegram-talk"
    target.mkdir(parents=True)
    for name in PLUGIN_FILES:
        src = ROOT / name
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, target / name)
    os.environ["HERMES_HOME"] = str(home)
    failures, evidence = 0, {}
    world = None
    try:
        llm = pulse_model.load_host()
        world = World(home, vb.Clock(float(int(time.time()))), model)
        world.llm = llm
        for fn in STEPS:
            try:
                evidence[fn.__name__] = fn(world)
                print(f"PASS [host] {fn.__name__}")
            except Exception as exc:
                failures += 1
                print(f"FAIL [host] {fn.__name__}: {type(exc).__name__}: {exc}")
        kinds = model.kinds()
        print("MODEL_REQUESTS " + json.dumps({k: kinds.count(k) for k in sorted(set(kinds))}, sort_keys=True))
    finally:
        if world is not None:
            world.close()
        model.close()
        shutil.rmtree(home, ignore_errors=True)
    print(f"{len(STEPS) - failures}/{len(STEPS)} engine steps passed [host]")
    print("ENGINE_EVIDENCE " + json.dumps(evidence, sort_keys=True, default=str))
    return failures


def run_offline():
    runtime = vb.hermes_python()
    if not runtime:
        print("FAILED: no Hermes runtime found; set HERMES_TALK_HERMES_PYTHON")
        print("TALK_PULSE_ENGINE_PARITY_FAILED")
        return 1
    tmp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    scratch = tempfile.mkdtemp(prefix="talk-pulse-engine-run-", dir=tmp)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HOME": scratch, "TMPDIR": scratch}
    try:
        host = subprocess.run([runtime, "-B", str(ROOT / "verify-pulse-engine.py"), "--host"], cwd=scratch, env=env,
                              capture_output=True, text=True, timeout=900)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"host runtime {runtime}")
    print(host.stdout.strip() or f"host run produced no output: {host.stderr[-1500:]}")
    if host.returncode != 0 and host.stderr.strip():
        print(host.stderr.strip()[-1500:])
    ok = host.returncode == 0
    print("TALK_PULSE_ENGINE_PARITY_OK" if ok else "TALK_PULSE_ENGINE_PARITY_FAILED")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--host", action="store_true", help="internal: run under a Hermes runtime")
    args = parser.parse_args()
    if args.host:
        return 1 if run_host() else 0
    return run_offline()


if __name__ == "__main__":
    sys.exit(main())
