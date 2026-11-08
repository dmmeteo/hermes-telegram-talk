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

    def issue(self, user=fx.USER_ID, thread_id=None):
        """What the gateway's /talk handler stores for a command in this user's private chat (or topic)."""
        return ThreadInbox(self.home, self.clock).issue_launch(
            Thread(self.config.profile_id, BOT_ID, user, str(user), thread_id))

    def start(self, user=fx.USER_ID, thread_id=None, launch=None, init=None, bound=True):
        """A Mini App launch from the /talk button of (user, thread); `launch`/`init` repeat an earlier launch."""
        init = init or sign(user, int(self.clock()), self.token)
        launch = launch or (self.issue(user, thread_id) if bound else None)
        boot = self.call("bootstrap", user, init=init)
        extra = {"launch": launch} if launch else {}
        answer = self.call("session", user, init=init, sdp=fx.OFFER, conversation_id=boot["conversation_id"], **extra)
        self.last_launch = (launch, init)
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


class MemoryExtras:
    """Stage-1 stand-ins for the gateway's /talk dispatch and a chat-agent turn's post_llm_call (see host.py)."""

    def __init__(self):
        self.origins = {}

    def talk_command(self, home, user_id, chat_id=None, chat_type="private", thread_id=None, bot_id=BOT_ID):
        from hermes_talk.thread_gateway import offer
        text, url = offer(home, bot_id, user_id, chat_id or user_id, chat_type, thread_id)
        return [] if text is None else [{"chat_id": int(chat_id or user_id), "text": text, "url": url,
                                         "message_thread_id": thread_id}]

    def payload(self, caption, paths):
        return caption + "\n\n" + "\n".join(f"[Image attached at: {p}]" for p in paths)

    def finalize(self, home, event, response, turn_id, history=(), api=None):
        """A chat-agent turn about `event`, shaped as the gateway shapes it: the model gets the multimodal payload
        (`api`, default: the event's caption plus its image hints), post_llm_call gets the clean persisted text (the
        caption; the payload itself when there is no caption) and the history ending in this turn's rows."""
        from types import SimpleNamespace
        from hermes_talk.thread_gateway import chat_reply
        sid = f"s-{event.source.user_id}-{event.source.chat_id}-{event.source.thread_id}"
        self.origins[sid] = event.source
        store = SimpleNamespace(lookup_by_session_id=lambda s: SimpleNamespace(origin=self.origins[s])
                                if s in self.origins else None)
        images = [u for u, t in zip(event.media_urls, event.media_types) if t.startswith("image/")]
        caption = event.text or getattr(getattr(event, "raw_message", None), "caption", None) or ""
        if api is None:
            api = self.payload(caption or "What do you see in this image?", images) if images else caption
        persisted = caption if caption else None
        messages = [*history, {"role": "user", "content": api}, {"role": "assistant", "content": response}]
        chat_reply(session_id=sid, platform="telegram", user_message=persisted if persisted is not None else api,
                   assistant_response=response, turn_id=turn_id, conversation_history=messages, home=str(home),
                   store=store)


def run(history_factory, event_factory, dispatch, out, extras=None):
    results = []
    extras = extras or MemoryExtras()

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

    @check("launch_binds_only_an_issued_handle_of_this_user_bot_and_profile_once")
    def _():
        rig = Rig(history_factory)
        try:
            inbox, profile = ThreadInbox(rig.home, rig.clock), rig.config.profile_id
            topic = rig.issue(thread_id="77")
            first = {"hash": "a" * 64}
            assert launch_thread(first, fx.USER_ID, profile, BOT_ID, inbox, topic) == (
                Thread(profile, BOT_ID, fx.USER_ID, str(fx.USER_ID), "77"), "bound")
            again = launch_thread(first, fx.USER_ID, profile, BOT_ID, inbox, topic)  # reconnect of the same page
            assert again[1] == "bound" and again[0].thread_id == "77", again
            assert launch_thread({"hash": "b" * 64}, fx.USER_ID, profile, BOT_ID, inbox, topic) == (None, "used")
            fresh = rig.issue()
            assert launch_thread({"hash": "c" * 64, "start_param": fresh}, fx.USER_ID, profile, BOT_ID, inbox)[1] == \
                "bound", "a signed start_param may carry the same handle"
            for fields, handle, why in (({}, None, "unbound"), ({"start_param": "topic77"}, None, "unbound"),
                                        ({"chat_type": "group"}, rig.issue(), "not_private"),
                                        ({"chat_type": "private"}, rig.issue(), "not_private"),
                                        ({}, "x" * 40, "invalid"), ({}, "../../etc", "invalid")):
                assert launch_thread({"hash": "d" * 64, **fields}, fx.USER_ID, profile, BOT_ID, inbox, handle) == (
                    None, why), (fields, handle)
            mine = rig.issue()
            assert launch_thread({"hash": "e" * 64}, fx.OTHER_USER_ID, profile, BOT_ID, inbox, mine) == (
                None, "foreign")
            assert launch_thread({"hash": "e" * 64}, fx.USER_ID, "other-profile", BOT_ID, inbox, mine) == (
                None, "foreign")
            assert launch_thread({"hash": "e" * 64}, fx.USER_ID, profile, "654321", inbox, mine) == (None, "foreign")
            late = rig.issue()
            rig.clock.now += 601
            assert launch_thread({"hash": "f" * 64}, fx.USER_ID, profile, BOT_ID, inbox, late) == (None, "expired")
            # the real routes: a menu launch without a handle is unbound and says so; it never takes main-chat input
            status = rig.call("voice")["thread"]["status"]
            assert status == "unbound", status
            ids = rig.start(bound=False)
            assert dispatch(rig.home, event_factory(text="Synthetic main chat", message_id=5)) is None
            assert "inputs" not in rig.context(ids)["sources"]
            assert rig.call("voice", launch=rig.issue())["thread"]["status"] == "bound"
            expect(400, lambda: rig.call("voice", launch=7))
            posts = len(rig.fake.posts)
            expect(400, lambda: rig.call("session", sdp=fx.OFFER, launch=["x"]))
            assert len(rig.fake.posts) == posts, "a malformed launch never reaches the provider"
        finally:
            rig.close()

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
            ids_topic = a.start(thread_id="12")
            assert dispatch(a.home, event_factory(text="for topic 12", thread_id="12", message_id=10)) == SKIP
            assert dispatch(a.home, event_factory(text="main lane", message_id=11)) is None  # no call there
            assert dispatch(b.home, event_factory(text="other profile", thread_id="12", message_id=12)) is None
            text = a.context(ids_topic)["text"]
            assert "for topic 12" in text and "main lane" not in text and "other profile" not in text, text
            ids_b = b.start(thread_id="12")
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
            launch, init = rig.last_launch
            second = rig.start(launch=launch, init=init)  # Retry in the same page: same launch, no api/end
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
                                  "choices": [{"id": "cove", "label": "Cove"}, {"id": "sol", "label": "Sol"}]}}
        rig = Rig(history_factory, data=data)
        try:
            assert "voice" not in rig.call("bootstrap")  # the frozen bootstrap shape is unchanged
            boot = rig.call("voice")
            assert boot["voice"]["current"] == "cove" and boot["voice"]["default"] == "cove"
            assert boot["voice"]["applies"] == "next_connection"
            assert {v["id"] for v in boot["voice"]["choices"]} == {
                "arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale"}
            assert rig.call("voice", voice="sol")["voice"]["current"] == "sol"
            expect(400, lambda: rig.call("voice", voice="shimmer"))
            expect(400, lambda: rig.call("voice", voice="marin"))  # an API-key voice is never offered
            expect(400, lambda: rig.call("voice", voice="sol", user_id=fx.OTHER_USER_ID))
            assert rig.call("voice", fx.OTHER_USER_ID)["voice"]["current"] == "cove"
            assert rig.call("voice")["voice"]["current"] == "sol"
            rig.start()
            assert rig.fake.posts[-1][2]["session"]["audio"]["output"]["voice"] == "sol"
            rig.start(fx.OTHER_USER_ID)
            assert rig.fake.posts[-1][2]["session"]["audio"]["output"]["voice"] == "cove"
        finally:
            rig.close()

    @check("shopy_is_female_and_restrained_pewds_stays_separate")
    def _():
        shopy = load_config(ROOT / "talk.shopy.example.yaml")
        pewds = load_config(ROOT / "talk.example.yaml")
        assert shopy.profile_id == "shopy" and shopy.self_reference == "feminine", shopy
        assert shopy.voice == "sol" and shopy.voice_choices[0][0] == "sol", shopy.voice_choices
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
            thread = Thread(rig.config.profile_id, BOT_ID, fx.USER_ID, str(fx.USER_ID))
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

    @check("talk_command_offers_the_mini_app_bound_to_its_own_thread")
    def _():
        rig = Rig(history_factory)
        try:
            sent = extras.talk_command(rig.home, fx.USER_ID, thread_id=12)
            assert len(sent) == 1 and sent[0]["chat_id"] == fx.USER_ID and sent[0]["message_thread_id"] == 12, sent
            url = sent[0]["url"]
            assert url.startswith(fx.ORIGIN + "/tg-voice/?launch="), url
            handle = url.split("launch=", 1)[1]
            ids = rig.start(launch=handle)
            assert dispatch(rig.home, event_factory(text="Synthetic topic note", thread_id="12", message_id=40)) == SKIP
            assert dispatch(rig.home, event_factory(text="Synthetic main note", message_id=41)) is None
            text = rig.context(ids)["text"]
            assert "Synthetic topic note" in text and "Synthetic main note" not in text, text
            expect_status = rig.call("voice", init=sign(fx.USER_ID, int(rig.clock()) - 1), launch=handle)
            assert expect_status["thread"]["status"] == "used", expect_status  # another launch cannot replay it
            assert extras.talk_command(rig.home, 999000111) == []  # not on the Talk allowlist
            group = extras.talk_command(rig.home, fx.USER_ID, chat_id=-100555, chat_type="supergroup")
            assert group and group[0]["url"] is None and "private chat" in group[0]["text"], group
            assert extras.talk_command(rig.home, 999000111, chat_id=-100555, chat_type="supergroup") == []
            (rig.home / "plugin-data" / "hermes-talk" / "threads" / "launch.json").unlink()
            assert extras.talk_command(rig.home, fx.USER_ID) == [], "no voice server: no answer and no handle"
        finally:
            rig.close()

    @check("chat_agent_reply_about_a_photo_reaches_the_same_call_attributed_once")
    def _():
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            photo = rig.root / "profiles" / "synthetic" / "photo.jpg"
            photo.write_bytes(JPEG)
            event = event_factory(message_id=60, message_type="photo", media=[(str(photo), "image/jpeg")],
                                  caption="Synthetic what is this")
            assert dispatch(rig.home, event) is None, "the photo goes on to the chat agent"
            message = extras.payload("Synthetic what is this", list(event.media_urls))
            if isinstance(message, list):  # the real multimodal payload carries the exact cached bytes
                import base64
                urls = [p["image_url"]["url"] for p in message if p.get("type") == "image_url"]
                assert len(urls) == 1 and base64.b64decode(urls[0].split(",", 1)[1]) == JPEG, urls
            reply = "Synthetic analysis: a test card with a grey square."
            # A newer unrelated turn whose history still holds the (not yet answered) photo row: only the turn's own
            # user row counts, so the photo's cached path in an older row must not attribute this reply.
            older = [{"role": "user", "content": message}, {"role": "assistant", "content": "Synthetic earlier"}]
            extras.finalize(rig.home, event_factory(text="Synthetic unrelated", message_id=61),
                            "Synthetic unrelated reply", "t-0", history=older)
            # The real route: post_llm_call gets the caption alone; the cached path is only in the turn's payload row.
            extras.finalize(rig.home, event, reply, "t-1", history=older)
            extras.finalize(rig.home, event, reply, "t-1", history=older)  # a repeated finalization of the same turn
            other = event_factory(message_id=60, message_type="photo", media=[(str(photo), "image/jpeg")],
                                  thread_id="99")
            extras.finalize(rig.home, other, "Synthetic foreign topic reply", "t-2", api=message)
            update = rig.context(ids)
            text = update["text"]
            assert text.count(reply) == 1 and "did not see it yourself" in text, text
            assert "Synthetic unrelated reply" not in text and "Synthetic foreign topic reply" not in text, text
            assert "photo" in text and "message 60" in text, text
            rig.ack(ids, update["revision"])
            again = rig.context(ids)
            assert reply not in (again.get("text") or ""), "delivered twice"
            kept = [t for _, t in rig.turns(ids["conversation_id"])]
            assert sum(reply in c for c in kept) == 1 and any(c.startswith("[Telegram chat agent reply") for c in kept)
            rig.call("end", **ids)
            extras.finalize(rig.home, event, "Synthetic late reply", "t-3")
            assert not any("Synthetic late reply" in c for c in
                           [t for _, t in rig.turns(ids["conversation_id"])]), "a stale reply after end"
        finally:
            rig.close()

    @check("uncaptioned_photo_reply_reaches_the_call_and_a_foreign_last_row_adds_nothing")
    def _():
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            photo = rig.root / "profiles" / "synthetic" / "bare.jpg"
            photo.write_bytes(JPEG)
            event = event_factory(message_id=62, message_type="photo", media=[(str(photo), "image/jpeg")])
            assert dispatch(rig.home, event) is None, "the photo goes on to the chat agent"
            second = rig.root / "profiles" / "synthetic" / "card2.jpg"  # each download is its own cached file
            second.write_bytes(JPEG)
            captioned = event_factory(message_id=63, message_type="photo", media=[(str(second), "image/jpeg")],
                                      caption="Synthetic second card")
            assert dispatch(rig.home, captioned) is None
            # A turn whose last user row is not its own input (caption absent from it) adds nothing from history.
            stray = extras.payload("Synthetic other note", list(captioned.media_urls))
            extras.finalize(rig.home, captioned, "Synthetic misattributed reply", "u-0", api=stray)
            extras.finalize(rig.home, event, "Synthetic bare analysis: a grey square.", "u-1")
            text = rig.context(ids)["text"]
            assert text.count("Synthetic bare analysis") == 1 and "message 62" in text, text
            assert "Synthetic misattributed reply" not in text, text
        finally:
            rig.close()

    @check("busy_thread_lock_never_stalls_the_gateway_or_drops_input")
    def _():
        import threading
        import time as _time
        rig = Rig(history_factory)
        try:
            ids = rig.start()
            held, release = threading.Event(), threading.Event()

            def hold():
                with ThreadInbox(rig.home).locked(key_main):
                    held.set()
                    release.wait(10)
            worker = threading.Thread(target=hold)
            worker.start()
            held.wait(5)
            began = _time.monotonic()
            result = dispatch(rig.home, event_factory(text="Synthetic during slow I/O", message_id=70))
            waited = _time.monotonic() - began
            release.set()
            worker.join(5)
            assert result is None and waited < 2.0, (result, waited)  # bounded; Hermes handles the message instead
            assert dispatch(rig.home, event_factory(text="Synthetic after the lock", message_id=71)) == SKIP
            text = rig.context(ids)["text"]
            assert "Synthetic after the lock" in text and "Synthetic during slow I/O" not in text, text
        finally:
            rig.close()

    @check("new_conversation_keeps_the_old_calls_unheard_input_in_the_old_history_once")
    def _():
        rig = Rig(history_factory)
        try:
            first = rig.start()
            assert dispatch(rig.home, event_factory(text="Synthetic orphan", message_id=80)) == SKIP
            fresh = rig.call("new", conversation_id=first["conversation_id"])["conversation_id"]
            assert fresh != first["conversation_id"]
            second = rig.start()  # no api/end for the first call; the new conversation binds the same thread
            assert second["conversation_id"] == fresh, (second, fresh)
            assert "inputs" not in rig.context(second)["sources"], "the old call's input never enters the new call"
            old = [t for _, t in rig.turns(first["conversation_id"])]
            assert sum("Synthetic orphan" in c for c in old) == 1, old
            assert not any("Synthetic orphan" in t for _, t in rig.turns(fresh))
            rig.start()  # binding again flushes nothing twice
            assert sum("Synthetic orphan" in t for _, t in rig.turns(first["conversation_id"])) == 1
            assert _inbox_names(rig, key_main) == []
        finally:
            rig.close()

    return results
