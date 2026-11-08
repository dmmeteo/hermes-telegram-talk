"""Host stage of verify-thread.py: the same checks with the real Hermes pieces (synthetic data only).

- History: real SessionDB (hermes_talk.history.NativeHistory) in each disposable profile home.
- Producer: Hermes' Telegram adapter builds each MessageEvent from a PTB 22 `Message` (Message.de_json with a PTB
  `Bot`); media go through Hermes' own cache functions (`cache_image_from_bytes_async`, `cache_media_bytes_async`)
  exactly as the adapter calls them after a download. Only the network download and `get_me` are replaced.
- Dispatch: the plugin is loaded by Hermes' PluginManager from a read-only copy in a disposable home, and every
  event goes through the gateway's own `GatewayRunner._hm_pre_gateway_dispatch_hook` (a bare runner, as the SDK's
  tests build it), with the profile home selected by `set_hermes_home_override`.
"""
import asyncio
import datetime
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "thread")]
PLUGIN_FILES = ("plugin.yaml", "__init__.py", "server.py", "hermes_talk", "static", "LICENSES")


def install_plugin(home):
    """The plugin as an immutable read-only directory copy, enabled in the home's config.yaml."""
    target = Path(home) / "plugins" / "hermes-telegram-talk"
    target.mkdir(parents=True)
    for name in PLUGIN_FILES:
        src = ROOT / name
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, target / name)
    for path in [*target.rglob("*"), target]:
        path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    (Path(home) / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-telegram-talk\n")


def load_plugin(home):
    """Discover the home's plugins the way a gateway does at startup; the Talk hook must be registered."""
    os.environ["HERMES_HOME"] = str(home)
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    discover_plugins()
    manager = get_plugin_manager()
    listed = {p["name"]: p for p in manager.list_plugins()}
    assert listed.get("hermes-telegram-talk", {}).get("enabled") and not listed["hermes-telegram-talk"].get("error"), listed
    hooks = manager._hooks.get("pre_gateway_dispatch") or []
    assert [h for h in hooks if h.__name__ == "_chat_input"], hooks
    return manager


class Producer:
    """Real Telegram adapter event construction (PTB 22 objects, Hermes' cache functions)."""

    def __init__(self):
        from gateway.config import PlatformConfig
        from plugins.platforms.telegram.adapter import TelegramAdapter
        self.adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123456:synthetic-offline-bot-token"))
        self.loop = asyncio.new_event_loop()

    def bot(self, bot_id):
        from telegram import Bot, User
        bot = Bot(token=f"{bot_id}:synthetic-offline-bot-token")
        bot._bot_user = User(id=int(bot_id), is_bot=True, first_name="Synthetic bot", username="synthetic_bot")
        return bot

    def __call__(self, text="", message_id=1, user_id=None, chat_id=None, chat_type="dm", thread_id=None,
                 bot_id="123456", internal=False, platform="telegram", message_type="text", media=(), caption=None,
                 edit_revision=None):
        from gateway.platforms.base import cache_image_from_bytes_async, cache_media_bytes_async
        from gateway.platforms.event import MessageType
        from hermes_talk import fixtures as fx
        from telegram import Message
        user_id = str(fx.USER_ID) if user_id is None else user_id
        chat_id = user_id if chat_id is None else chat_id
        chat = {"id": int(chat_id), "type": "private" if chat_type == "dm" else "supergroup"}
        if chat_type != "dm":
            chat["title"] = "Synthetic group"
        data = {"message_id": int(message_id), "date": 1_900_000_000, "chat": chat,
                "from": {"id": int(user_id), "is_bot": False, "first_name": "Test"}}
        if thread_id is not None:
            data.update(message_thread_id=int(thread_id), is_topic_message=True)
        if edit_revision is not None:
            data["edit_date"] = 1_900_000_100
        kind = {"text": MessageType.TEXT, "photo": MessageType.PHOTO, "document": MessageType.DOCUMENT}[message_type]
        if media:
            data["caption"] = caption if caption is not None else None
            if data["caption"] is None:
                del data["caption"]
        else:
            data["text"] = text
        message = Message.de_json(data, self.bot(bot_id) if bot_id else None)
        update_id = int(edit_revision) if edit_revision is not None else None
        event = self.adapter._build_message_event(message, kind, update_id=update_id)
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        for path, mime in media:  # what the adapter does with the downloaded bytes, in the profile's own home
            raw = Path(path).read_bytes()
            home = next((p for p in Path(path).parents if p.parent.name == "profiles"), None)
            if home is None:  # a file outside every profile home stays where it is (confinement check)
                event.media_urls.append(path)
                event.media_types.append(mime)
                continue
            token = set_hermes_home_override(home)
            if mime.startswith("image/") and message_type == "photo":
                cached = self.loop.run_until_complete(cache_image_from_bytes_async(raw, ext=Path(path).suffix))
                event.media_urls.append(cached)
                event.media_types.append(mime)
            else:
                cached = self.loop.run_until_complete(cache_media_bytes_async(raw, filename=Path(path).name,
                                                                              mime_type=mime))
                event.media_urls.append(cached.path if cached else path)
                event.media_types.append(cached.media_type if cached else mime)
            reset_hermes_home_override(token)
        if platform != "telegram":
            from gateway.config import Platform
            event.source.platform = Platform(platform)
        event.internal = internal
        return event


STORES = {}


def session_store(home):
    """The profile gateway's own SessionStore (real class, disposable sessions dir in that home)."""
    if str(home) not in STORES:
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        STORES[str(home)] = SessionStore(Path(home) / "sessions", GatewayConfig())
    return STORES[str(home)]


class HostExtras:
    """`/talk` through a real PTB Application wired by the Telegram adapter, and chat-agent turns finished by
    Hermes' own turn finalizer, so post_llm_call reaches the plugin through the registered hook."""

    def __init__(self, loop):
        self.loop = loop

    def _with_home(self, home, fn):
        previous = os.environ.get("HERMES_HOME")
        load_plugin(home)
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        token = set_hermes_home_override(home)
        try:
            return fn()
        finally:
            reset_hermes_home_override(token)
            if previous is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous

    def talk_command(self, home, user_id, chat_id=None, chat_type="private", thread_id=None, bot_id=None):
        import json
        from checks import BOT_ID
        from gateway.config import PlatformConfig
        from plugins.platforms.telegram.adapter import TelegramAdapter
        from telegram import Bot, Update
        from telegram.ext import ApplicationBuilder
        from telegram.request import BaseRequest
        bot_id = int(bot_id or BOT_ID)
        sent = []

        class Capture(BaseRequest):  # the disposable sender: Bot API calls are recorded, nothing leaves the host
            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            @property
            def read_timeout(self):
                return 1.0

            async def do_request(self, url, method, request_data=None, **_):
                name = url.rsplit("/", 1)[-1]
                params = dict(request_data.parameters) if request_data is not None else {}
                if name == "getMe":
                    result = {"id": bot_id, "is_bot": True, "first_name": "Synthetic bot", "username": "synthetic_bot"}
                else:
                    sent.append(params)
                    result = {"message_id": 9000 + len(sent), "date": 1_900_000_000, "text": params.get("text", ""),
                              "chat": {"id": int(params["chat_id"]), "type": "private"}}
                return 200, json.dumps({"ok": True, "result": result}).encode()

        def run():
            adapter = TelegramAdapter(PlatformConfig(enabled=True, token=f"{bot_id}:synthetic-offline-bot-token"))
            core = []

            async def core_command(update, context):
                core.append(update.effective_message.text)
            adapter._handle_command = core_command
            app = (ApplicationBuilder().bot(Bot(f"{bot_id}:synthetic-offline-bot-token", request=Capture()))
                   .updater(None).build())
            adapter._wire_plugin_handlers(app)  # what connect() does before registering the core handlers
            adapter._register_handlers(app)
            chat = {"id": int(chat_id or user_id), "type": chat_type}
            if chat_type != "private":
                chat["title"] = "Synthetic group"
            data = {"update_id": 1, "message": {"message_id": 70, "date": 1_900_000_000, "chat": chat, "text": "/talk",
                                                "from": {"id": int(user_id), "is_bot": False, "first_name": "Test"},
                                                "entities": [{"type": "bot_command", "offset": 0, "length": 5}]}}
            if thread_id is not None:
                data["message"].update(message_thread_id=int(thread_id), is_topic_message=True)

            async def go():
                await app.initialize()
                await app.process_update(Update.de_json(data, app.bot))
                await app.shutdown()
            self.loop.run_until_complete(go())
            assert core == [], "the core command handler must not also answer /talk"
        self._with_home(home, run)
        out = []
        for params in sent:
            markup = params.get("reply_markup")
            markup = json.loads(markup) if isinstance(markup, str) else markup
            url = markup["inline_keyboard"][0][0]["web_app"]["url"] if markup else None
            thread = params.get("message_thread_id")
            out.append({"chat_id": int(params["chat_id"]), "text": params["text"], "url": url,
                        "message_thread_id": int(thread) if thread is not None else None})
        return out

    def payload(self, caption, paths):
        from agent.image_routing import build_native_content_parts
        parts, skipped = build_native_content_parts(caption, list(paths))
        assert not skipped, skipped
        return parts

    def finalize(self, home, event, response, turn_id, history=(), api=None):
        """A chat-agent turn about `event` through the gateway's own preparation and run arguments: the clean
        persisted text from `_hmwa_apply_message_timestamp`, the model payload from `TurnRunner._native_image_run_message`
        (the event's images, as `_enrich_inbound_images` buffers them in native mode, unless `api` is given), the
        override selected as agent/turn_context.py selects `original_user_message`, then Hermes' own finalizer."""
        import logging
        from types import SimpleNamespace
        from agent.turn_finalizer import _apply_output_hooks
        from gateway.run import _event_media_is_image
        from gateway.run_turn_prepare import GatewayTurnPrepareMixin
        from gateway.run_turn_runner import TurnRunner
        entry = session_store(home).get_or_create_session(event.source)
        agent = SimpleNamespace(session_id=entry.session_id, model="synthetic-scripted-model", platform="telegram",
                                _persist_disabled=False)
        images = [u for i, u in enumerate(event.media_urls) if _event_media_is_image(event, i)]

        def run():
            message, persisted, _ = GatewayTurnPrepareMixin._hmwa_apply_message_timestamp(None, event, event.text)
            payload = api
            if payload is None:
                turn = object.__new__(TurnRunner)
                turn._ctx = SimpleNamespace(session_key=entry.session_key, message=message)
                turn._runner = SimpleNamespace(_consume_pending_native_image_paths=lambda key: list(images))
                payload = turn._native_image_run_message()
            original = persisted if persisted is not None else payload  # agent/turn_context.py
            messages = [*history, {"role": "user", "content": payload}, {"role": "assistant", "content": response}]
            return _apply_output_hooks(agent, response, logging.getLogger("talk-thread-host"), platform="telegram",
                                       effective_task_id=entry.session_id, turn_id=turn_id,
                                       original_user_message=original, messages=messages)
        self._with_home(home, run)


def make_dispatch(loop):
    from gateway.run import GatewayRunner
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    runner = object.__new__(GatewayRunner)  # bare runner, as the SDK's own dispatch tests build it

    def dispatch(home, event):
        runner.session_store = session_store(home)  # the profile gateway hands its own store to the hook
        # A profile's gateway runs with HERMES_HOME set to that profile's home; the hook resolves it the same way.
        previous = os.environ.get("HERMES_HOME")
        load_plugin(home)
        token = set_hermes_home_override(home)
        try:
            out = loop.run_until_complete(runner._hm_pre_gateway_dispatch_hook(event, event.source))
        finally:
            reset_hermes_home_override(token)
            if previous is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous
        if out is None:
            return {"action": "skip", "reason": "hermes-talk live call"}
        assert out is event, "the hook must never rewrite an event it does not take"
        return None
    return dispatch


def main():
    base = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    assert "/profiles/" not in str(base.resolve()) + "/", "TMPDIR must not be under a Hermes profiles directory"
    root = Path(tempfile.mkdtemp(prefix="talk-thread-host-", dir=base))
    try:
        import checks
        write_home = checks.fx.write_home

        def write_installed_home(home):  # every disposable profile home gets the installed, enabled plugin
            home = write_home(home)
            install_plugin(home)
            return home
        checks.fx.write_home = write_installed_home
        from hermes_talk.history import NativeHistory
        producer = Producer()
        dispatch = make_dispatch(producer.loop)
        results = checks.run(lambda home: NativeHistory(home), producer, dispatch, print, HostExtras(producer.loop))
        failed = [r for r in results if not r[1]]
        print(f"host stage: {len(results) - len(failed)}/{len(results)} checks passed")
        return 1 if failed else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
