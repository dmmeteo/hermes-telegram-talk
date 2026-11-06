"""Startup gate at the HTTP seam: opening a call reads prepared Pulse state and never waits for a scan or a model.

Runs the real server.py handler and TalkRuntime on an in-memory history with synthetic identities. The slow model is
a local stand-in for the host's ctx.llm; nothing reaches a provider.
Usage: python3 tests/startup/backend.py <repo>
"""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]).resolve()
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("verify_backend", ROOT / "verify-backend.py")
vb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vb)
fx = vb.fx

from hermes_talk.pulse_model import PulseModel  # noqa: E402

MODEL_DELAY_S = 2.0
PHRASES = ["Йо! Синтетичний профіль на зв’язку. Що мутимо?", "Привіт-привіт! Синтетика в ефірі. Що робимо?",
           "Хей! Тестовий голос на лінії. Кажи, що треба?"]
results, failures = [], []


class SlowLLM:
    """A ctx.llm stand-in whose every structured call takes MODEL_DELAY_S and then times out."""

    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def complete_structured(self, **_):
        with self.lock:
            self.calls += 1
        time.sleep(MODEL_DELAY_S)
        raise TimeoutError("synthetic slow route")


def check(fn):
    try:
        out = fn()
        results.append({"check": fn.__name__, "ok": True, **(out or {})})
    except Exception as exc:
        failures.append(fn.__name__)
        results.append({"check": fn.__name__, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return fn


def harness(**overrides):
    h = vb.Harness(False, fx.config_data(**overrides))
    h.llm = SlowLLM()
    h.talk.engine.model = PulseModel(llm=h.llm)
    h.talk.engine.start(delay=3600)
    return h


def close(h):
    h.talk.engine.stop()
    for thread in list(getattr(h.talk.engine, "preparing", {}).values()):
        thread.join(10)
    h.close()


def session(h, user=fx.USER_ID, conversation_id=None):
    started = time.monotonic()
    data = h.ok("session", {"sdp": fx.OFFER, **({"conversation_id": conversation_id} if conversation_id else {})}, user)
    return data, h.fake.posts[-1][2]["session"]["instructions"], time.monotonic() - started


def settle(h, user=fx.USER_ID):
    preparing = getattr(h.talk.engine, "preparing", {})
    thread = preparing.get(user)
    if thread is not None:
        thread.join(15)
    assert user not in preparing, "a background preparation outlived its run"


@check
def cold_slow_pulse_does_not_delay_the_call_and_catches_up_in_it():
    h = harness()
    try:
        h.seed("Synthetic late failure", turns=[("user", "go"), ("assistant", "boom")], failed=True, age_s=600)
        h.seed("Synthetic other user chat", source="telegram", user_id=str(fx.OTHER_USER_ID),
               turns=[("user", "private to someone else")], age_s=300)
        data, instructions, took = session(h)
        assert took < 1.0, f"call setup took {took:.2f}s"
        assert h.llm.calls <= 1 and "still being read" in instructions, "cold state must say it is still reading"
        assert "Synthetic late failure" not in instructions, "nothing is invented before the scan"
        settle(h)
        assert h.llm.calls >= 1, "the background preparation never asked the model"
        cid, vs = data["conversation_id"], data["voice_session_id"]
        update = h.ok("context", {"conversation_id": cid, "voice_session_id": vs})
        assert update["revision"] == 1 and "Synthetic late failure" in update["text"], update
        assert "someone else" not in update["text"] and "other user" not in update["text"]
        account = h.talk.account(fx.USER_ID)
        card = next(c for c in h.talk.pulse.snapshot(account)["items"].values() if "late failure" in c["title"])
        assert card["exposures"] == 0 and card["mentions"] == 0, "nothing counts before the ack"
        h.ok("context/ack", {"conversation_id": cid, "voice_session_id": vs, "revision": 1})
        card = h.talk.pulse.snapshot(account)["items"][card["fp"]]
        assert card["exposures"] == 1 and card["mentions"] == 0 and card["state"] != "heard", card["state"]
        again = h.ok("context", {"conversation_id": cid, "voice_session_id": vs})
        assert "Synthetic late failure" not in again["text"], "a delivered revision repeated in the same call"
        h.ok("events", {"conversation_id": cid, "voice_session_id": vs,
                        "events": [vb.ev("a1", 0, "assistant", "P1: the synthetic late failure failed."),
                                   vb.ev("u1", 1, "user", "ok")]})
        card = h.talk.pulse.snapshot(account)["items"][card["fp"]]
        assert card["mentions"] == 1, "a named, answered mention acknowledges the card"
        _, warm, took_warm = session(h, conversation_id=cid)
        assert took_warm < 1.0 and "still being read" not in warm, "the warm call reads the prepared state"
        return {"cold_setup_s": round(took, 3), "warm_setup_s": round(took_warm, 3), "model_calls": h.llm.calls}
    finally:
        close(h)


@check
def a_failing_background_preparation_leaves_the_call_usable():
    h = harness()
    try:
        def broken(user_id):
            raise RuntimeError("synthetic scan failure")
        h.talk.pulse_refresh = broken
        _, instructions, took = session(h)
        settle(h)
        assert took < 1.0 and "still being read" in instructions
        return {"setup_s": round(took, 3)}
    finally:
        close(h)


@check
def concurrent_calls_share_one_background_preparation():
    h = harness()
    try:
        h.seed("Synthetic job", turns=[("user", "go"), ("assistant", "ok")], end_reason="done", age_s=600)
        session(h)
        first = h.talk.engine.preparing.get(fx.USER_ID)
        session(h)
        assert first is not None and h.talk.engine.preparing.get(fx.USER_ID) in (first, None), "duplicate preparation"
        settle(h)
        return {}
    finally:
        close(h)


NATIVE_THREAD = threading.Thread


class _PausedFirstStart(NATIVE_THREAD):
    """Holds the first preparation thread between its registration and start() until the test lets it go."""
    created = []
    at_start = threading.Event()
    go = threading.Event()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created.append(self)

    def start(self):
        if self is self.created[0]:
            self.at_start.set()
            assert self.go.wait(10), "the first caller was never released"
        super().start()


@check
def a_same_account_call_inside_the_start_window_joins_the_registered_preparation():
    from hermes_talk import pulse_engine

    h = harness()
    refreshes, entered = [], threading.Event()
    release = threading.Event()

    def refresh(user_id):
        refreshes.append(user_id)
        entered.set()
        release.wait(10)
    h.talk.pulse_refresh = refresh
    _PausedFirstStart.created, _PausedFirstStart.at_start, _PausedFirstStart.go = [], threading.Event(), threading.Event()
    returned = {}
    first = threading.Thread(target=lambda: returned.update(first=h.talk.engine.prepare_soon(fx.USER_ID)))
    pulse_engine.threading.Thread = _PausedFirstStart
    try:
        first.start()
        assert _PausedFirstStart.at_start.wait(10), "the first caller never reached thread start"
        returned["second"] = h.talk.engine.prepare_soon(fx.USER_ID)
        _PausedFirstStart.go.set()
        first.join(10)
        assert entered.wait(10), "the registered preparation never ran"
    finally:
        pulse_engine.threading.Thread = NATIVE_THREAD
        _PausedFirstStart.go.set()
        release.set()
    try:
        settle(h)
        threads = len(_PausedFirstStart.created)
        assert (threads, refreshes) == (1, [fx.USER_ID]), f"{threads} threads, refreshes {refreshes}"
        assert returned["first"] is returned["second"], "the overlapping caller got another preparation"
        return {"threads": threads, "refreshes": len(refreshes)}
    finally:
        close(h)


@check
def a_preparation_whose_thread_cannot_start_leaves_the_account_retryable():
    from hermes_talk import pulse_engine

    h = harness()
    refreshes = []
    h.talk.pulse_refresh = refreshes.append

    class Refused(NATIVE_THREAD):
        def start(self):
            raise RuntimeError("can't start new thread")
    pulse_engine.threading.Thread = Refused
    try:
        refused = h.talk.engine.prepare_soon(fx.USER_ID)
    finally:
        pulse_engine.threading.Thread = NATIVE_THREAD
    try:
        assert refused is None and fx.USER_ID not in h.talk.engine.preparing, "a never-started thread kept the slot"
        h.talk.engine.prepare_soon(fx.USER_ID)
        settle(h)
        assert refreshes == [fx.USER_ID], refreshes
        h.talk.engine.prepare_soon(fx.USER_ID)
        settle(h)
        assert refreshes == [fx.USER_ID, fx.USER_ID], "a finished preparation blocks the next one"
        return {"refreshes": len(refreshes)}
    finally:
        close(h)


@check
def the_configured_ukrainian_greeting_is_short_varied_and_profile_scoped():
    h = harness(voice={"language_hint": "Speak Ukrainian unless the user switches language."},
                greeting={"enabled": True, "phrases": PHRASES})
    other = harness()
    try:
        used = []
        for _ in range(8):
            h.clock.now += 61
            _, instructions, _ = session(h)
            greeting = instructions.split("## Greeting\n", 1)[1]
            line = [p for p in PHRASES if p in greeting]
            assert len(line) == 1 and "mention at most" not in greeting and len(greeting) < 400, greeting
            assert "Speak Ukrainian" in instructions
            used.append(line[0])
        assert all(a != b for a, b in zip(used, used[1:])) and len(set(used)) >= 2, used
        _, plain, _ = session(other)
        assert not any(p in plain for p in PHRASES), "another profile inherited the greeting pool"
        settle(h)
        settle(other)
        return {"greetings": len(used), "distinct": len(set(used))}
    finally:
        close(h)
        close(other)


for result in results:
    print(json.dumps(result, ensure_ascii=False))
print(f"TALK_STARTUP_BACKEND_FAILED {failures}" if failures else "TALK_STARTUP_BACKEND_OK")
sys.exit(1 if failures else 0)
