#!/usr/bin/env python3
"""Talk Pulse acceptance: per-item briefing evidence through the real /tg-voice/ routes.

  python3 verify-pulse.py --offline

drives the real server with signed synthetic initData, a scripted upstream and synthetic sessions,
first on in-memory history and then on Hermes' own SessionDB under a real runtime
(HERMES_TALK_HERMES_PYTHON, else the first ~/.hermes/installs/*/environments/*/venv/bin/python).
A missing runtime is reported SKIPPED, never passed; HERMES_TALK_REQUIRE_HOST=1 makes it a failure.
Prints TALK_PULSE_OK when every check passed in every mode it ran.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("verify_backend", ROOT / "verify-backend.py")
vb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vb)
fx = vb.fx
from hermes_talk.talk import SNOOZE_S, TalkRuntime  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def ev(i, seq, role, text, interrupted=None):
    event = {"id": i, "sequence": seq, "role": role, "text": text}
    if interrupted is not None:
        event["interrupted"] = interrupted
    return event


def brief(instructions):
    if "## Activity since the last visit\n" not in instructions:
        return ""
    return instructions.split("## Activity since the last visit\n", 1)[1].split("\n\n## ", 1)[0]


def handles(instructions):
    """{title: "P<n>"} of the Pulse lines a call carried."""
    return {t: f"P{n}" for n, t in re.findall(r"^- P(\d+) \[\w+\] (.+?) \(\w+ session ", brief(instructions), re.M)}


class Pulse:
    def __init__(self, native, **budgets):
        data = fx.config_data(context={"skills": [], "budgets": {"activity_items": 3, **budgets}})
        self.h = vb.Harness(native, data)
        self.seq = 0

    def close(self):
        self.h.close()

    def cards(self, user=fx.USER_ID):
        return {c["title"]: c for c in self.h.talk.pulse.cards(self.h.talk.account(user))}

    def state(self, title, user=fx.USER_ID):
        return self.cards(user)[title]["state"]

    def call(self, user=fx.USER_ID, conversation_id=None):
        data, instructions = self.h.call(conversation_id, user=user)
        return data["conversation_id"], data["voice_session_id"], instructions

    def say(self, conv, vs, *turns, user=fx.USER_ID):
        events = []
        for turn in turns:
            role, text, *flag = turn
            events.append(ev(f"e{self.seq}", self.seq, role, text, *(flag or [None])))
            self.seq += 1
        return self.h.ok("events", {"conversation_id": conv, "voice_session_id": vs, "events": events}, user)

    def feedback(self, conv, vs, delegation, request, user=fx.USER_ID):
        return self.h.post("pulse/feedback", {"initData": self.h.init(user), "conversation_id": conv,
                                              "voice_session_id": vs, "delegation_item_id": delegation,
                                              "request": request})


def three(p):
    p.h.seed("Synthetic deploy check", turns=[("user", "go"), ("assistant", "deployed")], end_reason="done", age_s=900)
    p.h.seed("Synthetic broken backup", turns=[("user", "go"), ("assistant", "boom")], failed=True, age_s=800)
    p.h.seed("Synthetic invoice draft", turns=[("user", "go"), ("assistant", "drafted")], end_reason="done", age_s=700)


@check
def a_greeting_alone_consumes_no_briefing_item(p):
    three(p)
    conv, vs, first = p.call()
    assert set(handles(first)) == {"Synthetic deploy check", "Synthetic broken backup", "Synthetic invoice draft"}
    p.say(conv, vs, ("assistant", "Hello! A few things happened since we last spoke."), ("user", "hi"))
    assert {p.state(t) for t in handles(first)} == {"exposed"}, p.cards()
    assert p.h.post("end", {"initData": p.h.init(), "conversation_id": conv, "voice_session_id": vs})[0] == 200
    _, _, second = p.call()
    assert set(handles(second)) == set(handles(first)), "a greeting marked the whole briefing consumed"
    return {"exposed_after_greeting": sorted(handles(first))}


@check
def only_the_named_and_answered_item_is_acknowledged(p):
    three(p)
    conv, vs, first = p.call()
    p.say(conv, vs, ("assistant", "Hello."), ("assistant", "Your synthetic broken backup failed overnight."))
    assert p.state("Synthetic broken backup") == "mentioned", "output is not acknowledgement"
    p.say(conv, vs, ("user", "ugh, ok"))
    assert p.state("Synthetic broken backup") == "acknowledged"
    assert p.state("Synthetic deploy check") == p.state("Synthetic invoice draft") == "exposed"
    _, _, second = p.call()
    assert set(handles(second)) == {"Synthetic deploy check", "Synthetic invoice draft"}, brief(second)
    return {"acknowledged": "Synthetic broken backup", "still_unread": sorted(handles(second))}


@check
def a_mention_nobody_answered_returns_flagged_unconfirmed(p):
    three(p)
    conv, vs, _ = p.call()
    p.say(conv, vs, ("assistant", "The synthetic deploy check is done."))
    assert p.h.post("end", {"initData": p.h.init(), "conversation_id": conv, "voice_session_id": vs})[0] == 200
    assert p.state("Synthetic deploy check") == "mentioned"
    _, _, second = p.call()
    line = next(l for l in brief(second).splitlines() if "Synthetic deploy check" in l)
    assert "mentioned before, not confirmed heard" in line, line
    return {"abandoned_mention": line}


@check
def a_lone_update_paraphrased_still_counts_as_named(p):
    p.h.seed("Synthetic staging deploy for Acme", turns=[("user", "go"), ("assistant", "ok")], end_reason="done",
             age_s=300)
    conv, vs, _ = p.call()
    p.say(conv, vs, ("assistant", "Hi! The Acme staging deploy went through."), ("user", "nice"))
    assert p.state("Synthetic staging deploy for Acme") == "acknowledged"
    return {"paraphrase": "Acme staging deploy", "state": "acknowledged"}


@check
def an_interrupted_mention_is_not_evidence(p):
    three(p)
    conv, vs, _ = p.call()
    p.say(conv, vs, ("assistant", "Your synthetic invoice draft is", True), ("user", "wait, stop"))
    card = p.cards()["Synthetic invoice draft"]
    assert card["state"] == "exposed" and card["interrupted"] == 1 and card["mentions"] == 0, card
    p.say(conv, vs, ("assistant", "Sorry. The synthetic invoice draft is ready."), ("user", "great"))
    assert p.state("Synthetic invoice draft") == "acknowledged"
    return {"interrupted": 1, "then_acknowledged": True}


@check
def omitted_and_clipped_items_are_never_exposed(p):
    p.h.config = p.h.talk.config = vb.parse_config(fx.config_data(context={"skills": [], "budgets": {
        "activity_items": 1}}), p.h.home)
    three(p)
    conv, vs, first = p.call()
    assert list(handles(first)) == ["Synthetic broken backup"], "failures are selected first"
    assert "2 more update(s) not listed yet" in brief(first)
    assert p.state("Synthetic deploy check") == p.state("Synthetic invoice draft") == "candidate"
    return {"selected": list(handles(first)), "waiting": 2}


@check
def restart_reopen_and_new_conversation_keep_the_account_pulse(p):
    three(p)
    conv, vs, _ = p.call()
    p.say(conv, vs, ("assistant", "The synthetic deploy check went fine."), ("user", "nice"))
    before = p.cards()
    p.h.talk = TalkRuntime(p.h.config, fx.BOT_TOKEN, p.h.history, soul=p.h.talk.soul, skill=p.h.talk.skill,
                           clock=p.h.clock)
    p.h.app.talk = p.h.talk
    assert {t: c["state"] for t, c in p.cards().items()} == {t: c["state"] for t, c in before.items()}
    new = p.h.ok("new", {"conversation_id": conv})["conversation_id"]
    assert new != conv
    _, _, fresh = p.call(conversation_id=new)
    assert set(handles(fresh)) == {"Synthetic broken backup", "Synthetic invoice draft"}, brief(fresh)
    return {"restarted": True, "new_conversation_keeps_unread": sorted(handles(fresh))}


@check
def feedback_is_idempotent_and_changes_selection(p):
    three(p)
    p.h.seed("Synthetic cron rotation", source="cron", turns=[("user", "go"), ("assistant", "rotated")],
             end_reason="done", age_s=600)
    conv, vs, first = p.call()
    tag = handles(first)
    assert len(tag) == 3 and "Synthetic cron rotation" not in tag
    deploy = tag["Synthetic deploy check"]
    once = p.feedback(conv, vs, "del_fb_1", f"pulse {deploy} not useful")
    again = p.feedback(conv, vs, "del_fb_1", f"pulse {deploy} not useful")
    assert once[0] == again[0] == 200 and once[1] == again[1], (once, again)
    assert once[1]["card"]["state"] == "dismissed" and once[1]["channel"] == "speakable"
    memory = json.loads(p.h.history.get_meta(f"hermes_talk:pulse:{p.h.talk.account(fx.USER_ID).key}"))["memory"]
    assert memory["sources"]["cli"] == {"not_useful": 1}, memory["sources"]
    later = p.feedback(conv, vs, "del_fb_2", f"pulse {tag['Synthetic invoice draft']} later")[1]
    assert later["card"]["state"] == "snoozed"
    more = p.feedback(conv, vs, "del_fb_3", f"pulse {tag['Synthetic broken backup']} more")[1]
    assert "Synthetic broken backup" in more["reply"] and more["card"]["state"] == "acknowledged"
    assert p.feedback(conv, vs, "del_fb_4", "pulse P9 useful")[0] == 404
    assert p.feedback(conv, vs, "del_fb_5", "pulse please")[0] == 400
    _, vs2, second = p.call()
    assert list(handles(second)) == ["Synthetic cron rotation"], brief(second)
    p.h.clock.now += SNOOZE_S + 1
    _, _, third = p.call()
    line = next(l for l in brief(third).splitlines() if "Synthetic invoice draft" in l)
    assert "follow-up the user asked for" in line and line.startswith("- P1 "), brief(third)
    assert p.h.post("end", {"initData": p.h.init(), "conversation_id": conv, "voice_session_id": vs})[0] == 200
    assert p.feedback(conv, vs, "del_fb_6", "pulse P1 useful")[0] == 409, "feedback needs the live call"
    return {"idempotent": True, "dismissed": "Synthetic deploy check", "snoozed_then_due": line,
            "expanded": more["reply"][:80]}


@check
def a_learned_dislike_ranks_a_source_lower(p):
    p.h.config = p.h.talk.config = vb.parse_config(fx.config_data(context={"skills": [], "budgets": {
        "activity_items": 1}}), p.h.home)
    p.h.seed("Synthetic cli first", turns=[("user", "go"), ("assistant", "ok")], end_reason="done", age_s=900)
    p.h.seed("Synthetic cron later", source="cron", turns=[("user", "go"), ("assistant", "ok")], end_reason="done",
             age_s=800)
    conv, vs, first = p.call()
    assert list(handles(first)) == ["Synthetic cli first"], "equal importance drains oldest first"
    assert p.feedback(conv, vs, "del_rank", "pulse P1 not useful")[0] == 200
    p.h.seed("Synthetic cli again", turns=[("user", "go"), ("assistant", "ok")], end_reason="done", age_s=850)
    _, _, second = p.call()
    assert list(handles(second)) == ["Synthetic cron later"], brief(second)
    return {"after_dislike": list(handles(second))}


@check
def the_same_event_is_told_once(p):
    sid = p.h.seed("Synthetic nightly build", turns=[("user", "go")], age_s=60)
    conv, vs, first = p.call()
    assert "[active] Synthetic nightly build" in brief(first)
    p.say(conv, vs, ("assistant", "The synthetic nightly build is running."), ("user", "ok"))
    _, _, again = p.call()
    assert "Synthetic nightly build" not in brief(again), "an unchanged acknowledged item came back"
    p.h.clock.now += 5
    if p.h.native:
        p.h.history.db.append_message(sid, "assistant", content="built", timestamp=p.h.clock())
        p.h.history.db.end_session(sid, "done")
    else:
        p.h.history.append(sid, [{"role": "assistant", "content": "built", "platform_message_id": None,
                                  "timestamp": p.h.clock()}])
        p.h.history.sessions[sid]["last_active"] = p.h.clock()
        p.h.history.end_session(sid, "done", p.h.clock())
    conv3, vs3, third = p.call()
    lines = [l for l in brief(third).splitlines() if "Synthetic nightly build" in l]
    assert len(lines) == 1 and "[replied]" in lines[0] and "follow-up on an earlier update" in lines[0], brief(third)
    p.say(conv3, vs3, ("assistant", "The synthetic nightly build finished."), ("user", "good"))
    p.h.clock.now += 5
    p.h.seed("Synthetic Nightly Build!", source="cron", turns=[("user", "go"), ("assistant", "built")],
             end_reason="done", age_s=0)
    _, _, fourth = p.call()
    assert "Nightly Build" not in brief(fourth), "the same news from another session was told twice"
    states = sorted(c["state"] for c in p.h.talk.pulse.cards(p.h.talk.account(fx.USER_ID)))
    assert states == ["acknowledged", "acknowledged", "duplicate"], states
    return {"follow_up": lines[0], "states": states}


@check
def a_mid_call_update_is_exposed_only_when_appended(p):
    conv, vs, first = p.call()
    assert brief(first) == ""
    p.h.seed("Synthetic late alert", turns=[("user", "go"), ("assistant", "alerted")], failed=True, age_s=0)
    status, update = p.h.post("context", {"initData": p.h.init(), "conversation_id": conv, "voice_session_id": vs})
    assert status == 200 and "- P1 [failed] Synthetic late alert" in update["text"], update
    assert p.state("Synthetic late alert") == "candidate", "an update not appended exposes nothing"
    assert p.h.post("context/ack", {"initData": p.h.init(), "conversation_id": conv, "voice_session_id": vs,
                                    "revision": update["revision"]})[0] == 200
    assert p.state("Synthetic late alert") == "exposed"
    p.say(conv, vs, ("assistant", "Heads up: the synthetic late alert failed."), ("user", "on it"))
    assert p.state("Synthetic late alert") == "acknowledged"
    return {"revision": update["revision"], "acknowledged_mid_call": True}


@check
def accounts_never_share_pulse_cards_or_feedback(p):
    p.h.seed("Synthetic owner secret", turns=[("user", "go"), ("assistant", "ok")], end_reason="done", age_s=100)
    p.h.seed("Synthetic other chat", source="telegram", user_id=str(fx.OTHER_USER_ID),
             turns=[("user", "go"), ("assistant", "ok")], age_s=90)
    conv, vs, mine = p.call()
    oconv, ovs, theirs = p.call(user=fx.OTHER_USER_ID)
    assert "Synthetic owner secret" in handles(mine) and "Synthetic other chat" not in handles(mine)
    assert list(handles(theirs)) == ["Synthetic other chat"]
    assert p.feedback(conv, vs, "del_x", "pulse P1 dismiss", user=fx.OTHER_USER_ID)[0] == 404
    assert p.feedback(oconv, ovs, "del_y", "pulse P1 dismiss")[0] == 404
    p.say(oconv, ovs, ("assistant", "Synthetic owner secret is done."), ("user", "ok"), user=fx.OTHER_USER_ID)
    assert p.state("Synthetic owner secret") == "exposed" and "Synthetic owner secret" not in p.cards(fx.OTHER_USER_ID)
    return {"isolated": True}


def run(native):
    label = "host" if native else "memory"
    failures, evidence = 0, {}
    for fn in CHECKS:
        p = Pulse(native)
        try:
            evidence[fn.__name__] = fn(p)
            print(f"PASS [{label}] {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL [{label}] {fn.__name__}: {type(exc).__name__}: {exc}")
        finally:
            p.close()
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} pulse checks passed [{label}]")
    print("PULSE_EVIDENCE " + json.dumps({label: evidence}, sort_keys=True, default=str))
    return failures


def run_offline():
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    memory_failures = run(native=False)
    runtime = vb.hermes_python()
    if not runtime:
        print("SKIPPED host Pulse checks: no Hermes runtime found; set HERMES_TALK_HERMES_PYTHON")
        host_ok = os.environ.get("HERMES_TALK_REQUIRE_HOST") != "1"
    else:
        scratch = tempfile.mkdtemp(prefix="talk-pulse-host-")
        try:
            host = subprocess.run([runtime, "-B", str(ROOT / "verify-pulse.py"), "--host"], cwd=scratch,
                                  env={**env, "HERMES_HOME": scratch}, capture_output=True, text=True, timeout=600)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        print(f"host runtime {runtime}")
        print(host.stdout.strip() or f"host run produced no output: {host.stderr[-800:]}")
        host_ok = host.returncode == 0
    ok = memory_failures == 0 and host_ok
    print(("TALK_PULSE_OK" + ("" if runtime else " (host SKIPPED)")) if ok else "TALK_PULSE_FAILED")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--host", action="store_true", help="internal: run under a Hermes runtime")
    args = parser.parse_args()
    if args.host:
        return 1 if run(native=True) else 0
    return run_offline()


if __name__ == "__main__":
    sys.exit(main())
