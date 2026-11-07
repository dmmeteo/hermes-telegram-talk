"""Exact-thread chat input, reply voice and Shopy persona checks (synthetic identities and text only).

`run(history_factory, event_factory, dispatch)` runs every check against one history backend:
- history_factory(home) -> MemoryHistory or NativeHistory (real SessionDB);
- event_factory(**fields) -> an inbound MessageEvent (a duck-typed stand-in, or the real adapter-built one);
- dispatch(home, event) -> the pre_gateway_dispatch result (the hook body, or the real registered gateway hook).
"""
import hashlib
import hmac
import json
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from hermes_talk import fixtures as fx  # noqa: E402
from hermes_talk.config import load_config, parse_config  # noqa: E402
from hermes_talk.context import compose  # noqa: E402
from hermes_talk.talk import TalkRuntime  # noqa: E402
from hermes_talk.thread_inbox import Thread, ThreadInbox, launch_thread  # noqa: E402

BOT_ID = fx.BOT_TOKEN.split(":")[0]
OTHER_BOT_TOKEN = "654321:synthetic-other-bot-token"
SKIP = {"action": "skip", "reason": "hermes-talk live call"}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def sign(user_id, auth_date, token=fx.BOT_TOKEN, **extra):
    fields = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id, "first_name": "Test"},
                                                              separators=(",", ":")), **extra}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


class Rig:
    """One profile home: real routes (VoiceApp.handle), the Talk runtime and a fake provider."""

    def __init__(self, history_factory, profile="synthetic", data=None, token=fx.BOT_TOKEN, root=None):
        self.root = Path(root or tempfile.mkdtemp(prefix="talk-thread-"))
        self.home = fx.write_home(self.root / "profiles" / profile)
        self.clock = Clock(__import__("time").time())
        self.fake = fx.FakeUpstream()
        self.token = token
        data = data or fx.config_data()
        data = {**data, "profile": {**data["profile"], "id": profile}}
        self.config = parse_config(data, self.home)
        self.history = history_factory(self.home)
        self.talk = TalkRuntime(self.config, token, self.history, soul=fx.file_soul(self.home),
                                skill=fx.file_skill(self.home), clock=self.clock, home=self.home)
        self.app = server.VoiceApp(token, {fx.USER_ID, fx.OTHER_USER_ID}, fx.ORIGIN, "/tg-voice/",
                                   resolve=self.fake.resolve, post=self.fake.post, clock=self.clock, talk=self.talk)

    def call(self, route, user=fx.USER_ID, init=None, **body):
        body = {"initData": init or sign(user, int(self.clock()), self.token), **body}
        return self.app.handle(route, json.dumps(body).encode())

    def start(self, user=fx.USER_ID, **launch):
        boot = self.call("bootstrap", user, init=sign(user, int(self.clock()), self.token, **launch))
        answer = self.call("session", user, init=sign(user, int(self.clock()), self.token, **launch),
                           sdp=fx.OFFER, conversation_id=boot["conversation_id"])
        return {"conversation_id": answer["conversation_id"], "voice_session_id": answer["voice_session_id"]}

    def context(self, ids, user=fx.USER_ID):
        return self.call("context", user, **ids)

    def ack(self, ids, revision, user=fx.USER_ID):
        return self.call("context/ack", user, revision=revision, **ids)

    def turns(self, conversation_id):
        return self.history.recent(conversation_id, 50)

    def close(self):
        close = getattr(self.history, "close", None)
        if close:
            close()
        shutil.rmtree(self.root, ignore_errors=True)


def expect(status, fn):
    try:
        fn()
    except server.ApiError as exc:
        assert exc.status == status, (exc.status, exc.message)
        return
    raise AssertionError(f"expected HTTP {status}")


def _inbox_names(rig, thread_key):
    return [n for n, _ in ThreadInbox(rig.home).pending(thread_key)]


def run(history_factory, event_factory, dispatch, out):
    results = []

    def check(name):
        def wrap(fn):
            try:
                fn()
                results.append((name, True, ""))
                out(f"PASS {name}")
            except Exception as exc:  # report every failing check with its reason
                import traceback
                where = traceback.extract_tb(exc.__traceback__)[-1].lineno
                results.append((name, False, f"{type(exc).__name__} at line {where}: {exc}"))
                out(f"FAIL {name}: {results[-1][2]}")
            return fn
        return wrap

    key_main = f"tg{BOT_ID}:{fx.USER_ID}:{fx.USER_ID}:-"

    @check("launch_thread_binds_only_signed_private_chat_and_topic")
    def _():
        t = launch_thread({}, fx.USER_ID, "p", BOT_ID)
        assert t == Thread("p", BOT_ID, fx.USER_ID, str(fx.USER_ID), None), t
        assert launch_thread({"chat_type": "sender"}, fx.USER_ID, "p", BOT_ID).thread_id is None
        assert launch_thread({"start_param": "topic77"}, fx.USER_ID, "p", BOT_ID).thread_id == "77"
        for refused in ({"chat_type": "group"}, {"chat_type": "supergroup"}, {"chat_type": "channel"},
                        {"chat_type": "private"}, {"start_param": "chat-999"}, {"start_param": "topic7x"}):
            assert launch_thread(refused, fx.USER_ID, "p", BOT_ID) is None, refused

    @check("text_during_call_reaches_same_call_once_without_reconnect")
    def _():
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            assert dispatch(rig.home, event_factory(text="Synthetic: buy oat milk", message_id=501)) == SKIP
            assert dispatch(rig.home, event_factory(text="Synthetic: buy oat milk", message_id=501)) == SKIP
            first = rig.context(ids)
            assert first["sources"]["inputs"] == 1 and "C1 text" in first["text"], first
            assert first["text"].count("buy oat milk") == 1, first["text"]
            again = rig.context(ids)  # not acked yet (e.g. a dropped append): offered again, never lost
            assert again["sources"]["inputs"] == 1 and again["revision"] != first["revision"], again
            rig.ack(ids, again["revision"])
            rig.ack(ids, again["revision"])
            assert "inputs" not in rig.context(ids)["sources"]
            kept = [t for _, t in rig.turns(ids["conversation_id"]) if "buy oat milk" in t]
            assert len(kept) == 1 and kept[0].startswith("[Telegram chat input during the call]"), kept
            # The same voice session is still live: nothing reconnected.
            rig.call("events", conversation_id=ids["conversation_id"], voice_session_id=ids["voice_session_id"],
                     events=[{"id": "u1", "sequence": 0, "role": "user", "text": "Synthetic spoken turn"}])
        finally:
            rig.close()

    @check("forged_or_foreign_input_is_never_taken")
    def _():
        rig = Rig(history_factory)
        try:
            rig.start()
            refused = [
                dict(text="other user", user_id=str(fx.OTHER_USER_ID), chat_id=str(fx.OTHER_USER_ID)),
                dict(text="other chat", chat_id="-100777"),
                dict(text="group with same user", chat_type="group", chat_id=str(fx.USER_ID)),
                dict(text="other topic", thread_id="55"),
                dict(text="other bot", bot_id="654321"),
                dict(text="unproven bot", bot_id=None),
                dict(text="internal", internal=True),
                dict(text="/stop"),
                dict(text="discord", platform="discord"),
            ]
            for n, fields in enumerate(refused):
                result = dispatch(rig.home, event_factory(message_id=600 + n, **fields))
                assert result is None, (fields, result)
            assert _inbox_names(rig, key_main) == [], _inbox_names(rig, key_main)
        finally:
            rig.close()

    @check("browser_cannot_choose_chat_or_another_users_call")
    def _():
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            expect(400, lambda: rig.call("context", chat_id="-100777", **ids))
            expect(404, lambda: rig.call("context", user=fx.OTHER_USER_ID, **ids))
            expect(401, lambda: rig.app.handle("context", json.dumps({"initData": sign(fx.USER_ID, int(
                rig.clock()), OTHER_BOT_TOKEN), **ids}).encode()))
        finally:
            rig.close()

    @check("topics_and_profiles_are_isolated")
    def _():
        a = Rig(history_factory, "alpha")
        b = Rig(history_factory, "beta", root=a.root / "b")
        try:
            ids_topic = a.start(start_param="topic12")
            assert dispatch(a.home, event_factory(text="for topic 12", thread_id="12", message_id=10)) == SKIP
            assert dispatch(a.home, event_factory(text="main lane", message_id=11)) is None  # no call there
            assert dispatch(b.home, event_factory(text="other profile", thread_id="12", message_id=12)) is None
            text = a.context(ids_topic)["text"]
            assert "for topic 12" in text and "main lane" not in text and "other profile" not in text, text
            ids_b = b.start(start_param="topic12")
            assert "inputs" not in b.context(ids_b)["sources"]
        finally:
            b.close()
            a.close()

    @check("photo_is_truthful_notice_and_passes_to_chat_agent")
    def _():
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            photo = rig.home / "cache" / "images" / "img_1.jpg"
            photo.parent.mkdir(parents=True)
            photo.write_bytes(JPEG)
            result = dispatch(rig.home, event_factory(text="what is this?", message_id=700, message_type="photo",
                                                      media=[(str(photo), "image/jpeg")], caption="what is this?"))
            assert result is None, result  # the chat agent's real multimodal path keeps it
            text = rig.context(ids)["text"]
            assert "a photo (JPEG" in text and "cannot see" in text and 'caption "what is this?"' in text, text
            assert "Do not describe its contents" in text, text
        finally:
            rig.close()

    @check("text_file_is_decoded_and_bad_files_are_explicit")
    def _():
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            docs = rig.home / "cache" / "documents"
            docs.mkdir(parents=True)
            (docs / "notes.txt").write_text("Synthetic note: line one\nряд два\n", encoding="utf-8")
            (docs / "big.txt").write_text("x" * (70 * 1024))
            (docs / "latin1.txt").write_bytes("caf\xe9".encode("latin-1"))
            (docs / "report.pdf").write_bytes(b"%PDF-1.4 synthetic")
            (docs / "fake.png").write_bytes(b"not an image")
            outside = Path(tempfile.mkdtemp()) / "outside.txt"
            outside.write_text("outside home")
            cases = [("notes.txt", "text/plain", SKIP), ("big.txt", "text/plain", None),
                     ("latin1.txt", "text/plain", None), ("report.pdf", "application/pdf", None),
                     ("fake.png", "image/png", None)]
            for n, (name, mime, want) in enumerate(cases):
                got = dispatch(rig.home, event_factory(text="", message_id=800 + n, message_type="document",
                                                       media=[(str(docs / name), mime)]))
                assert got == want, (name, got)
            assert dispatch(rig.home, event_factory(text="", message_id=899, message_type="document",
                                                    media=[(str(outside), "text/plain")])) is None
            shutil.rmtree(outside.parent, ignore_errors=True)
            text = rig.context(ids)["text"]
            assert "Synthetic note: line one\nряд два" in text, text
            for needle in ("larger than 64 KB", "not UTF-8 text", "report.pdf", "a file type the call cannot read",
                           "fake.png is not a readable image", "could not be read"):
                assert needle in text, (needle, text)
            assert "outside home" not in text
        finally:
            rig.close()

    @check("two_turn_reopen_keeps_order_and_current_input_once")
    def _():
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            conv, voice = ids["conversation_id"], ids["voice_session_id"]
            rig.call("events", conversation_id=conv, voice_session_id=voice,
                     events=[{"id": "a1", "sequence": 0, "role": "user", "text": "Synthetic first question"}])
            assert dispatch(rig.home, event_factory(text="Synthetic typed follow-up", message_id=900)) == SKIP
            update = rig.context(ids)
            rig.ack(ids, update["revision"])
            rig.call("events", conversation_id=conv, voice_session_id=voice,
                     events=[{"id": "a2", "sequence": 1, "role": "assistant", "text": "Synthetic answer"}])
            rig.call("end", conversation_id=conv, voice_session_id=voice)
            assert dispatch(rig.home, event_factory(text="Synthetic sent after ending", message_id=901)) is None  # unbound
            reopened = rig.start()
            assert reopened["conversation_id"] == conv
            _, instructions, _, _ = rig.talk.prepare(fx.USER_ID, conv)
            recent = instructions.split("## Most recent turns", 1)[1].split("\n## ", 1)[0]
            order = [recent.find(s) for s in ("user: Synthetic first question", "user: [Telegram chat input during "
                                              "the call] text", "assistant: Synthetic answer")]
            assert -1 not in order and order == sorted(order), (order, recent)
            assert recent.count("Synthetic typed follow-up") == 1, recent
            stored = [t for _, t in rig.turns(conv)]
            assert sum("Synthetic typed follow-up" in t for t in stored) == 1, stored
            assert "Synthetic sent after ending" not in instructions
        finally:
            rig.close()

    @check("minimize_restore_retry_keeps_input_without_double_injection")
    def _():
        rig = Rig(history_factory)
        try:
            first = rig.start()
            assert dispatch(rig.home, event_factory(text="Synthetic while minimized", message_id=950)) == SKIP
            fetched = rig.context(first)  # fetched but the page was hidden before its append was acked
            assert fetched["sources"]["inputs"] == 1
            second = rig.start()  # Retry: a new voice session of the same conversation and thread, no api/end
            assert second["conversation_id"] == first["conversation_id"]
            assert dispatch(rig.home, event_factory(text="Synthetic after restore", message_id=951)) == SKIP
            update = rig.context(second)
            assert update["text"].count("Synthetic while minimized") == 1 and "Synthetic after restore" in update[
                "text"], update
            assert update["text"].index("while minimized") < update["text"].index("after restore")
            rig.ack(second, update["revision"])
            expect(404, lambda: rig.ack(first, fetched["revision"] + 99))
            assert dispatch(rig.home, event_factory(text="Synthetic for the live call", message_id=952)) == SKIP
            rig.call("end", **first)  # the stale call ends late: it neither stores twice nor takes the live call's input
            late = rig.context(second)
            assert late["sources"]["inputs"] == 1 and "Synthetic for the live call" in late["text"], late
            rig.ack(second, late["revision"])
            rig.call("end", **second)
            texts = [t for _, t in rig.turns(first["conversation_id"])]
            assert sum("while minimized" in t for t in texts) == 1 and sum("after restore" in t for t in texts) == 1
            assert sum("for the live call" in t for t in texts) == 1, texts
        finally:
            rig.close()

    @check("undelivered_input_is_kept_at_end_and_edits_are_distinct")
    def _():
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            assert dispatch(rig.home, event_factory(text="Synthetic orig", message_id=960)) == SKIP
            assert dispatch(rig.home, event_factory(text="Synthetic fixed", message_id=960, edit_revision="77")) == SKIP
            rig.call("end", **ids)
            texts = [t for _, t in rig.turns(ids["conversation_id"])]
            assert any("Synthetic orig" in t for t in texts) and any("(edited message)" in t and "Synthetic fixed" in t
                                                                     for t in texts), texts
            assert _inbox_names(rig, key_main) == []
        finally:
            rig.close()

    @check("expired_binding_takes_nothing")
    def _():
        rig = Rig(history_factory)
        try:
            rig.start()
            rig.clock.now += rig.config.voice_session_ttl_s + 5
            import time as _time
            from hermes_talk.thread_inbox import take_event
            assert take_event(event_factory(text="late", message_id=970), rig.home,
                              clock=lambda: rig.clock.now) is None
            assert _time.time() > 0
        finally:
            rig.close()

    @check("reply_voice_is_persisted_per_account_and_applies_next_connection")
    def _():
        data = fx.config_data()
        data = {**data, "voice": {**data.get("voice", {}), "voice": "cove",
                                  "choices": [{"id": "cove", "label": "Cove"}, {"id": "marin", "label": "Marin"}]}}
        rig = Rig(history_factory, data=data)
        try:
            assert "voice" not in rig.call("bootstrap")  # the frozen bootstrap shape is unchanged
            boot = rig.call("voice")
            assert boot["voice"] == {"current": "cove", "default": "cove", "applies": "next_connection",
                                     "choices": [{"id": "cove", "label": "Cove"}, {"id": "marin", "label": "Marin"}]}
            assert rig.call("voice", voice="marin")["voice"]["current"] == "marin"
            expect(400, lambda: rig.call("voice", voice="shimmer"))
            expect(400, lambda: rig.call("voice", voice="marin", user_id=fx.OTHER_USER_ID))
            assert rig.call("voice", fx.OTHER_USER_ID)["voice"]["current"] == "cove"
            assert rig.call("voice")["voice"]["current"] == "marin"
            rig.start()
            assert rig.fake.posts[-1][2]["session"]["audio"]["output"]["voice"] == "marin"
            rig.start(fx.OTHER_USER_ID)
            assert rig.fake.posts[-1][2]["session"]["audio"]["output"]["voice"] == "cove"
        finally:
            rig.close()

    @check("shopy_is_female_and_restrained_pewds_stays_separate")
    def _():
        shopy = load_config(ROOT / "talk.shopy.example.yaml")
        pewds = load_config(ROOT / "talk.example.yaml")
        assert shopy.profile_id == "shopy" and shopy.self_reference == "feminine", shopy
        assert shopy.voice == "marin" and shopy.voice_choices[0][0] == "marin", shopy.voice_choices
        assert pewds.voice == "cove" and pewds.self_reference == "" and pewds.profile_id == "pewds"
        meme = ("pewds", "ligma", "бро", "братан", "йоу", "кринж", "лол")
        for line in shopy.greeting_phrases + (shopy.greeting,):
            assert len(line) <= 90 and not any(m in line.lower() for m in meme), line
        assert any(w in " ".join(shopy.greeting_phrases) for w in ("готова", "рада", "корисною")), \
            shopy.greeting_phrases
        text, _ = compose(shopy, "", [], "", [], [], 0, "", greeting_line=shopy.greeting_phrases[0])
        assert "feminine grammatical gender" in text and "я готова" in text, text[:600]
        ptext, _ = compose(pewds, "", [], "", [], [], 0, "")
        assert "feminine" not in ptext and "masculine" not in ptext

    @check("stale_end_overlapping_new_bind_never_deletes_the_new_binding")
    def _():
        # Deterministic interleaving: the old call's end has read the binding (still its own) and is paused before
        # removing it; a new call of the same thread binds in between. The new binding must survive.
        import threading
        rig = Rig(history_factory)
        try:
            thread = launch_thread({}, fx.USER_ID, rig.config.profile_id, BOT_ID)
            old = ThreadInbox(rig.home, rig.clock)
            old.bind(thread, "conv-A", "voice-A", 600)
            read, go = threading.Event(), threading.Event()
            real = old.binding

            def paused(key, live_only=False):
                data = real(key, live_only)
                read.set()
                go.wait(5)
                return data
            old.binding = paused
            ender = threading.Thread(target=old.unbind, args=(thread, "voice-A"))
            ender.start()
            assert read.wait(5)
            binder = threading.Thread(target=ThreadInbox(rig.home, rig.clock).bind,
                                      args=(thread, "conv-B", "voice-B", 600))
            binder.start()
            binder.join(1.0)  # without the lifecycle lock this bind completes here, before the stale unlink
            go.set()
            ender.join(5)
            binder.join(5)
            assert not ender.is_alive() and not binder.is_alive()
            now = ThreadInbox(rig.home, rig.clock).binding(thread.key, live_only=True)
            assert now is not None and now["voice_session_id"] == "voice-B", now
            # and the later, now stale, end of A still leaves B alone
            assert ThreadInbox(rig.home, rig.clock).unbind(thread, "voice-A") is False
            assert ThreadInbox(rig.home, rig.clock).binding(thread.key)["voice_session_id"] == "voice-B"
        finally:
            rig.close()

    @check("take_racing_end_is_kept_once_never_stranded")
    def _():
        # The gateway has read a live binding and is paused before storing; the call ends meanwhile. The input
        # must end up in the conversation exactly once (or pass to the chat agent), never skipped and stranded.
        import threading
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            read, go, result = threading.Event(), threading.Event(), []
            real = ThreadInbox.binding

            def paused(self, key, live_only=False):
                data = real(self, key, live_only)
                if threading.current_thread().name == "take" and live_only:
                    read.set()
                    go.wait(5)
                return data
            ThreadInbox.binding = paused
            try:
                taker = threading.Thread(name="take", target=lambda: result.append(
                    dispatch(rig.home, event_factory(text="Synthetic racing input", message_id=990))))
                taker.start()
                assert read.wait(5)
                ender = threading.Thread(target=lambda: rig.call("end", **ids))
                ender.start()
                ender.join(1.0)  # without the lock the end flushes and unbinds here, before the input is stored
                go.set()
                taker.join(5)
                ender.join(5)
            finally:
                ThreadInbox.binding = real
            texts = [t for _, t in rig.turns(ids["conversation_id"])]
            kept = sum("Synthetic racing input" in t for t in texts)
            stranded = _inbox_names(rig, key_main)
            assert stranded == [], stranded
            assert (result == [SKIP] and kept == 1) or (result == [None] and kept == 0), (result, kept, texts)
        finally:
            rig.close()

    @check("unheard_input_of_an_unended_call_reaches_the_reopened_call_once")
    def _():
        rig = Rig(history_factory)
        try:
            first = rig.start()
            assert dispatch(rig.home, event_factory(text="Synthetic unheard", message_id=995)) == SKIP
            # the page closed without api/end; the same thread reopens (same conversation) and binds again
            again = rig.start()
            assert again["conversation_id"] == first["conversation_id"]
            update = rig.context(again)
            assert update["text"].count("Synthetic unheard") == 1, update["text"]
            rig.ack(again, update["revision"])
            rig.call("end", **first)  # the stale end of the first call changes nothing
            rig.call("end", **again)
            texts = [t for _, t in rig.turns(first["conversation_id"])]
            assert sum("Synthetic unheard" in t for t in texts) == 1, texts
            assert _inbox_names(rig, key_main) == []
        finally:
            rig.close()

    @check("oversized_leading_input_cannot_starve_later_messages")
    def _():
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            docs = rig.home / "cache" / "documents"
            docs.mkdir(parents=True, exist_ok=True)
            for n in (1, 2):
                (docs / f"big{n}.txt").write_text(("Synthetic line %d. " % n) * 300, encoding="utf-8")
            album = event_factory(message_id=1001, message_type="document",
                                  media=[(str(docs / "big1.txt"), "text/plain"), (str(docs / "big2.txt"), "text/plain")])
            assert dispatch(rig.home, album) is None  # an album goes on to the chat agent as well
            assert dispatch(rig.home, event_factory(text="Synthetic after album", message_id=1002)) == SKIP
            seen = []
            for _ in range(3):
                update = rig.context(ids)
                if not update.get("revision"):
                    break
                seen.append(update["text"])
                rig.ack(ids, update["revision"])
            joined = "\n".join(seen)
            assert "more characters of this input were not carried" in joined, joined[:300]
            assert sum("Synthetic after album" in t for t in seen) == 1, len(seen)
            assert _inbox_names(rig, key_main) == []
        finally:
            rig.close()

    return results
