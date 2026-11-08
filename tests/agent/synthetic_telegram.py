"""Synthetic stand-in for Hermes' Telegram Bot API call, for offline report checks. Never contacts Telegram.

Installed over tools.send_message_tool._send_telegram, the function Hermes' platform sender
(_send_to_platform) calls for Telegram, so Talk's sender, Hermes' platform routing and its result
contract run unchanged. It returns the dicts Hermes' own _send_telegram returns: success with the
Bot API message id, or "Telegram send failed: <python-telegram-bot error text>". python-telegram-bot
itself is not installed in the test runtime and is not exercised.
"""
import asyncio
import threading

OUTCOMES = {
    "timeout": {"error": "Telegram send failed: Timed out"},
    "refused": {"error": "Telegram send failed: httpx.ConnectError: [Errno 111] Connection refused"},
    "chat_not_found": {"error": "Telegram send failed: Chat not found"},
    "bad_gateway": {"error": "Telegram send failed: Bad Gateway"},
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
        import tools.send_message_tool as native
        native._send_telegram = self.send

    def sent(self):
        return [c for c in self.calls if c["outcome"] == "ok"]

    async def send(self, token, chat_id, message, media_files=None, thread_id=None, disable_link_previews=False,
                   force_document=False):
        with self.lock:
            outcome = self.script.pop(0) if self.script else "ok"
            call = {"token": token, "chat_id": chat_id, "thread_id": thread_id, "text": message,
                    "media": [path for path, _ in media_files or []], "force_document": force_document,
                    "outcome": outcome, "message_id": None}
            self.calls.append(call)
        if self.on_call:
            self.on_call(call)
        if outcome == "hang":
            await asyncio.sleep(3600)
        if outcome in OUTCOMES:
            return dict(OUTCOMES[outcome])
        call["message_id"] = str(4000 + len(self.calls))
        return {"success": True, "platform": "telegram", "chat_id": chat_id, "message_id": call["message_id"]}
