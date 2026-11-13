"""Synthetic stand-in for Hermes' Telegram Bot API calls, for offline report checks. Never contacts Telegram.

Installed over tools.send_message_tool._send_telegram, the function Hermes' platform sender
(_send_to_platform) calls for Telegram, and over tools.send_message_senders._telegram_bot, the Bot factory
Talk edits a status message with. Talk's sender, Hermes' platform routing and its result contract run
unchanged. A send returns the dicts Hermes' own _send_telegram returns: success with the Bot API message
id, or "Telegram send failed: <python-telegram-bot error text>". An edit raises python-telegram-bot's error
text, as Bot.edit_message_text does.
"""
import asyncio
import threading

OUTCOMES = {
    "timeout": {"error": "Telegram send failed: Timed out"},
    "refused": {"error": "Telegram send failed: httpx.ConnectError: [Errno 111] Connection refused"},
    "chat_not_found": {"error": "Telegram send failed: Chat not found"},
    "bad_gateway": {"error": "Telegram send failed: Bad Gateway"},
}
EDIT_ERRORS = {
    "timeout": "Timed out",
    "not_modified": "Message is not modified: specified new message content and reply markup are exactly the same",
    "gone": "Message to edit not found",
}


class SyntheticTelegram:
    def __init__(self):
        self.calls = []
        self.script = []
        self.lock = threading.Lock()
        self.on_call = None

    def reset(self, *script):
        with self.lock:
            self.calls, self.script = [], list(script)

    def install(self):
        import tools.send_message_senders as senders
        import tools.send_message_tool as native
        native._send_telegram = self.send
        senders._telegram_bot = lambda token: SyntheticBot(self, token)

    def sent(self):
        return [c for c in self.calls if c["kind"] == "send" and c["outcome"] == "ok"]

    def edits(self):
        return [c for c in self.calls if c["kind"] == "edit" and c["outcome"] == "ok"]

    def record(self, call):
        with self.lock:
            call["outcome"] = self.script.pop(0) if self.script else "ok"
            self.calls.append(call)
        if self.on_call:
            self.on_call(call)
        return call

    async def send(self, token, chat_id, message, media_files=None, thread_id=None, disable_link_previews=False,
                   force_document=False):
        call = self.record({"kind": "send", "token": token, "chat_id": chat_id, "thread_id": thread_id,
                            "text": message, "media": [path for path, _ in media_files or []], "message_id": None})
        if call["outcome"] == "hang":
            await asyncio.sleep(3600)
        if call["outcome"] in OUTCOMES:
            return dict(OUTCOMES[call["outcome"]])
        call["message_id"] = str(4000 + len(self.calls))
        return {"success": True, "platform": "telegram", "chat_id": chat_id, "message_id": call["message_id"]}


class SyntheticBot:
    def __init__(self, telegram, token):
        self.telegram, self.token = telegram, token

    async def edit_message_text(self, chat_id, message_id, text):
        call = self.telegram.record({"kind": "edit", "token": self.token, "chat_id": chat_id,
                                     "message_id": str(message_id), "text": text})
        if call["outcome"] == "hang":
            await asyncio.sleep(3600)
        if call["outcome"] in EDIT_ERRORS:
            raise RuntimeError(EDIT_ERRORS[call["outcome"]])
        return True
