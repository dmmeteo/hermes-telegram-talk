"""Synthetic stand-in for the Bot API behind Talk's status messages, for offline report checks. Never contacts Telegram.

Installed over hermes_talk.receipts._open, so Talk's own sender builds the real request and maps the real
answer: a success is the Bot API's message object, a refusal is urllib's HTTPError carrying Telegram's JSON
error, and a lost connection or a timeout is the exception urllib raises for it. Each call is recorded from
the request itself (method and token from the URL, fields from the JSON body); a request with any field but
the text and its address (a parse mode, media, markup) is refused.
"""
import io
import json
import threading
import time
import urllib.error

from hermes_talk import receipts

REFUSALS = {"chat_not_found": (400, "Bad Request: chat not found")}
KINDS = {"sendMessage": "send", "editMessageText": "edit"}
FIELDS = {"sendMessage": {"chat_id", "text", "message_thread_id"},
          "editMessageText": {"chat_id", "message_id", "text"}}


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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
        receipts._open = self.open

    def sent(self):
        return [c for c in self.calls if c["kind"] == "send" and c["outcome"] == "ok"]

    def edits(self):
        return [c for c in self.calls if c["kind"] == "edit" and c["outcome"] == "ok"]

    def open(self, request, timeout):
        assert request.full_url.startswith(receipts.BOT_API + "/bot"), "only the Bot API is called"
        token, method = request.full_url[len(receipts.BOT_API) + 4:].rsplit("/", 1)
        fields = json.loads(request.data)
        assert set(fields) <= FIELDS[method], f"a status message is plain text only: {sorted(fields)}"
        call = {"kind": KINDS[method], "token": token, "text": fields.pop("text"), **fields}
        if call["kind"] == "send":
            call.update(chat_id=str(call["chat_id"]), thread_id=call.pop("message_thread_id", None), message_id=None)
        else:
            call["message_id"] = str(call["message_id"])
        with self.lock:
            call["outcome"] = outcome = self.script.pop(0) if self.script else "ok"
            self.calls.append(call)
            number = len(self.calls)
        if self.on_call:
            self.on_call(call)
        if outcome == "hang":
            time.sleep(timeout)
        if outcome in ("hang", "timeout"):
            raise TimeoutError("timed out")
        if outcome == "refused":
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        if outcome in REFUSALS:
            status, description = REFUSALS[outcome]
            body = json.dumps({"ok": False, "error_code": status, "description": description}).encode()
            raise urllib.error.HTTPError(request.full_url, status, "refused", {}, io.BytesIO(body))
        if call["kind"] == "send":
            call["message_id"] = str(4000 + number)
        return Reply(json.dumps({"ok": True, "result": {"message_id": int(call["message_id"])}}).encode())
