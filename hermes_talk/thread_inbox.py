"""Exact-thread chat input for a live Talk call.

A Talk call continues one Telegram conversation: (profile, bot, user, chat, thread). The voice server records that
binding when a call starts (`ThreadInbox.bind`). The profile's gateway runs this plugin's `pre_gateway_dispatch`
hook (registered in `__init__.register`) on every inbound MessageEvent, before Hermes' own auth. The hook takes an
event only when its bot, chat, thread and sender equal a live binding of this HERMES_HOME; everything else passes
untouched.

What the call can genuinely consume (text, small UTF-8 text files) is written durably to the binding's inbox and
the event is skipped, so the voice is its only consumer. What it cannot (photos, PDFs, voice notes, anything
oversized or undecodable) is recorded as a truthful notice and the event goes on to the profile's ordinary chat
agent, which has the real multimodal path. If writing fails the event passes untouched, so no input is lost.

The voice server reads pending items of its own binding in `api/context`, appends them once (acked) to the
conversation through SessionDB with a `talk:chat:...` receipt, and flushes the rest into history at `api/end`.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

DATA_DIR = ("plugin-data", "hermes-talk", "threads")
TEXT_CHARS = 4000            # one Telegram text message (4096 UTF-16 units) plus slack
FILE_BYTES = 64 * 1024       # a text file the call reads whole; larger ones go to the chat agent
FILE_CHARS = 6000            # decoded characters a call receives from one file
IMAGE_BYTES = 20 * 1024 * 1024
CAPTION_CHARS = 1000
DONE_KEEP_S = 7 * 24 * 3600  # settled records kept against a late redelivery of the same message
PENDING_MAX = 50             # undelivered items per binding; later ones pass to the chat agent
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".jsonl", ".xml", ".yaml", ".yml",
                   ".toml", ".ini", ".cfg", ".conf", ".srt", ".vtt", ".html", ".htm", ".py", ".js", ".ts", ".sh",
                   ".sql", ".diff", ".patch", ".rst", ".tex"}
TOPIC = re.compile(r"topic(\d{1,12})")
DIGITS = re.compile(r"-?\d{1,20}")
ITEM = re.compile(r"m(\d{1,20})(?:-e([A-Za-z0-9_-]{1,40}))?\.json")


@dataclass(frozen=True)
class Thread:
    """The exact Telegram conversation a call continues. Chat and thread come only from signed launch data."""
    profile: str
    bot_id: str
    user_id: int
    chat_id: str
    thread_id: str = None

    @property
    def key(self):
        return f"tg{self.bot_id}:{self.user_id}:{self.chat_id}:{self.thread_id or '-'}"

    @property
    def label(self):
        return "this chat" if self.thread_id is None else f"topic {self.thread_id} of this chat"


def launch_thread(fields, user_id, profile, bot_id):
    """The Thread a Mini App launch proves, or None.

    `fields` are the already HMAC-verified initData fields. Only a launch inside the user's private chat with this
    bot binds (menu/keyboard/inline buttons, which carry no `chat_type`, or a direct link with `chat_type=sender`),
    so the chat id is the user id. A Telegram-signed `start_param` of the form `topic<id>` names a topic of that same
    private chat. Group, channel and other-user chats are never bound: Telegram does not sign their chat id there."""
    chat_type = fields.get("chat_type")
    if chat_type not in (None, "sender"):
        return None
    start = fields.get("start_param")
    topic = TOPIC.fullmatch(start) if isinstance(start, str) else None
    if start not in (None, "") and topic is None:
        return None
    return Thread(profile, str(bot_id), int(user_id), str(user_id), topic.group(1) if topic else None)


def _digest(key):
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _write_new(path, data):
    """Create `path` with `data` exactly once (fsynced); False when it already exists."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    with open(tmp, "x", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def _write(path, data):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    with open(tmp, "x", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class ThreadInbox:
    """Binding files and per-binding inboxes under the profile's plugin-data; shared by gateway and voice server."""

    def __init__(self, home, clock=time.time):
        self.root = Path(home).joinpath(*DATA_DIR)
        self.clock = clock

    def _bind_path(self, key):
        return self.root / f"bind-{_digest(key)}.json"

    def _inbox(self, key):
        return self.root / f"inbox-{_digest(key)}"

    @contextlib.contextmanager
    def locked(self, key):
        """Hold this thread's lifecycle lock across processes (voice server and gateway).

        Bind, end and take each read the binding and then act on it; under one exclusive `flock` per thread key a
        stale call's end can never delete a newer call's binding, and the gateway can never store input for a
        binding that ends (and flushes) in between. The lock file itself is never removed."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.root / f"lock-{_digest(key)}", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the flock

    # Voice-server side ------------------------------------------------------------------------------------------
    def bind(self, thread, conversation_id, voice_session_id, ttl_s):
        """Make this call the thread's live binding; returns input earlier calls took but never settled.

        The returned [(name, item)] belong to other conversations (their call ended without api/end); the caller
        keeps them in their own conversation's history so nothing is stranded by the newer binding."""
        with self.locked(thread.key):
            self._inbox(thread.key).mkdir(exist_ok=True, mode=0o700)
            self._prune(thread.key)
            _write(self._bind_path(thread.key), {**asdict(thread), "key": thread.key,
                                                 "conversation_id": conversation_id,
                                                 "voice_session_id": voice_session_id,
                                                 "expires_at": self.clock() + ttl_s})
            return [(n, i) for n, i in self.pending(thread.key) if i.get("conversation_id") != conversation_id]

    def unbind(self, thread, voice_session_id, flush=None):
        """End the binding only if it still belongs to this call (a newer call of the same thread keeps its own).

        True when this call owned (or nobody held) the binding. `flush(items)` then receives this call's pending
        input under the same lock, so a concurrent take either lands before the flush or sees no binding."""
        with self.locked(thread.key):
            current = self.binding(thread.key)
            owned = current is None or current.get("voice_session_id") == voice_session_id
            if current is not None and owned:
                self._bind_path(thread.key).unlink(missing_ok=True)
            if owned and flush is not None:
                flush(self.pending(thread.key))
            return owned

    def binding(self, key, live_only=False):
        try:
            data = json.loads(self._bind_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("key") != key:
            return None
        if live_only and not data.get("expires_at", 0) > self.clock():
            return None
        return data

    def _prune(self, key):
        """Forget settled records after DONE_KEEP_S; they only guard against a late redelivery of the same message."""
        cutoff = self.clock() - DONE_KEEP_S
        for path in self._inbox(key).glob("m*.done"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def pending(self, key):
        """Undelivered items of one binding, oldest Telegram message first: [(name, item)]."""
        folder = self._inbox(key)
        out = []
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        for name in names:
            match = ITEM.fullmatch(name)
            if match is None:
                continue
            try:
                item = json.loads((folder / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(item, dict) and item.get("key") == key:
                out.append(((int(match.group(1)), match.group(2) or "", name), name, item))
        return [(name, item) for _, name, item in sorted(out)]

    def settle(self, key, name):
        """Mark one item delivered (kept as `.done` for provenance); repeated settles change nothing."""
        if ITEM.fullmatch(name) is None:
            return
        path = self._inbox(key) / name
        try:
            os.replace(path, path.with_suffix(".done"))
        except FileNotFoundError:
            pass

    # Gateway side ------------------------------------------------------------------------------------------------
    def offer(self, thread_key, item):
        """Store one taken input once; True if stored now or already stored, False if the inbox is full."""
        folder = self._inbox(thread_key)
        name = f"m{item['message_id']}" + (f"-e{item['revision']}" if item.get("revision") else "") + ".json"
        if (folder / name).exists() or (folder / name).with_suffix(".done").exists():
            return True
        if len(self.pending(thread_key)) >= PENDING_MAX:
            return False
        _write_new(folder / name, item)
        return True


# --- the gateway hook ------------------------------------------------------------------------------------------------

def _value(obj, name):
    value = getattr(obj, name, None)
    return getattr(value, "value", value)


def _event_bot_id(event):
    """The receiving bot from the adapter's own update object; None when it cannot be proven."""
    raw = getattr(event, "raw_message", None)
    get_bot = getattr(raw, "get_bot", None)
    try:
        bot = get_bot() if callable(get_bot) else None
    except Exception:  # PTB raises when no bot is attached: unproven, so the event is not taken
        return None
    bot_id = getattr(bot, "id", None)
    return str(bot_id) if isinstance(bot_id, int) else None


def _confined(path, home):
    """The real path of a cached attachment inside this HERMES_HOME, else None."""
    try:
        real = Path(path).resolve(strict=True)
        root = Path(home).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return None
    return real if real.is_file() and root in real.parents else None


def _image_format(head):
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _kib(size):
    return f"{max(1, round(size / 1024))} KB"


def describe_attachment(path, mime, home):
    """(kind, take, detail) for one cached attachment. Only a whole, decodable text file is taken."""
    real = _confined(path, home)
    if real is None:
        return "unreadable", False, {"reason": "the attachment file is not readable here"}
    size = real.stat().st_size
    name = re.sub(r"[^\w.\- ]", "_", real.name)[-80:]
    suffix = real.suffix.lower()
    mime = mime if isinstance(mime, str) else ""
    with open(real, "rb") as handle:
        head = handle.read(16)
    image = _image_format(head)
    if image is not None or mime.startswith("image/"):
        if image is None:
            return "unreadable", False, {"reason": f"{name} is not a readable image", "name": name}
        if size > IMAGE_BYTES:
            return "image", False, {"format": image, "bytes": size, "reason": "oversized"}
        return "image", False, {"format": image, "bytes": size}
    if suffix in TEXT_EXTENSIONS or mime.startswith("text/"):
        if size > FILE_BYTES:
            return "file", False, {"name": name, "bytes": size, "reason": f"larger than {_kib(FILE_BYTES)}"}
        try:
            text = real.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return "file", False, {"name": name, "bytes": size, "reason": "not UTF-8 text"}
        clipped = len(text) > FILE_CHARS
        return "file", True, {"name": name, "bytes": size, "text": text[:FILE_CHARS], "chars": len(text),
                              "clipped": clipped}
    return "file", False, {"name": name, "bytes": size, "mime": mime or "unknown",
                           "reason": "a file type the call cannot read"}


def take_event(event, home, clock=time.time):
    """The gateway hook body: {"action": "skip"} when the call consumed the event, else None (normal dispatch)."""
    source = getattr(event, "source", None)
    if source is None or getattr(event, "internal", False) or _value(source, "platform") != "telegram":
        return None
    if getattr(source, "chat_type", None) != "dm" or getattr(source, "is_bot", False):
        return None
    user_id, chat_id = getattr(source, "user_id", None), getattr(source, "chat_id", None)
    message_id = getattr(event, "message_id", None)
    if not (isinstance(user_id, str) and DIGITS.fullmatch(user_id) and isinstance(chat_id, str)
            and DIGITS.fullmatch(chat_id) and isinstance(message_id, str) and message_id.isdigit()):
        return None
    message_type = _value(event, "message_type")
    text = getattr(event, "text", "") or ""
    if message_type == "command" or (message_type == "text" and text.lstrip().startswith("/")):
        return None  # gateway commands (/stop, /new ...) stay Hermes' own
    bot_id = _event_bot_id(event)
    if bot_id is None:
        return None
    thread_id = getattr(source, "thread_id", None)
    key = f"tg{bot_id}:{user_id}:{chat_id}:{thread_id or '-'}"
    inbox = ThreadInbox(home, clock)
    if not inbox.root.is_dir():
        return None  # no call of this profile ever bound a thread
    with inbox.locked(key):
        return _take_locked(inbox, key, event, user_id, chat_id, message_id, message_type, text, clock, home)


def _take_locked(inbox, key, event, user_id, chat_id, message_id, message_type, text, clock, home):
    """Check the live binding and store the input under the thread lock (see ThreadInbox.locked)."""
    binding = inbox.binding(key, live_only=True)
    if binding is None or str(binding.get("user_id")) != user_id or binding.get("chat_id") != chat_id:
        return None
    revision = getattr(event, "edit_revision", None)
    item = {"key": key, "message_id": int(message_id), "at": clock(),
            "revision": re.sub(r"[^A-Za-z0-9_-]", "_", str(revision))[:40] if revision else None,
            "conversation_id": binding["conversation_id"], "voice_session_id": binding["voice_session_id"]}
    media = list(getattr(event, "media_urls", None) or [])
    types = list(getattr(event, "media_types", None) or [])
    raw = getattr(event, "raw_message", None)
    caption = getattr(raw, "caption", None)
    try:
        if not media:
            if message_type != "text" or not text.strip():
                inbox.offer(key, {**item, "kind": "unsupported", "taken": False,
                                  "detail": {"type": str(message_type), "reason": "a message type the call cannot use"}})
                return None
            if len(text) > TEXT_CHARS:
                inbox.offer(key, {**item, "kind": "text", "taken": False,
                                  "detail": {"chars": len(text), "reason": "too long for the call"}})
                return None
            return {"action": "skip", "reason": "hermes-talk live call"} if inbox.offer(
                key, {**item, "kind": "text", "taken": True, "text": text}) else None
        caption = caption if isinstance(caption, str) else ""
        details = [describe_attachment(p, types[i] if i < len(types) else "", home) for i, p in enumerate(media)]
        take = len(details) == 1 and details[0][1]
        record = {**item, "kind": details[0][0] if len(details) == 1 else "album", "taken": take,
                  "caption": caption[:CAPTION_CHARS],
                  "attachments": [dict(d, kind=k) for k, _, d in details]}
        stored = inbox.offer(key, record)
        return {"action": "skip", "reason": "hermes-talk live call"} if take and stored else None
    except OSError:
        return None  # the call never saw it; Hermes handles it as usual


def gateway_hook(event=None, **_):
    """`pre_gateway_dispatch` callback (registered in `__init__.register`)."""
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    except Exception:
        home = os.environ.get("HERMES_HOME")
    if not home or event is None:
        return None
    try:
        return take_event(event, home)
    except Exception:  # a plugin fault must never drop or block a message
        return None


# --- the voice-server side -------------------------------------------------------------------------------------------

def receipt(item):
    revision = f":e{item['revision']}" if item.get("revision") else ""
    return f"talk:chat:{item['key']}:{item['message_id']}{revision}"


def item_line(handle, item):
    """One context line for the voice model. Never claims the model saw bytes it did not get."""
    when = time.strftime("%H:%M", time.gmtime(item.get("at") or 0))
    edited = " (edited message)" if item.get("revision") else ""
    if item.get("kind") == "text" and item.get("taken"):
        return f"- {handle} text{edited} at {when} UTC: {item['text']}"
    if item.get("kind") == "text":
        return (f"- {handle} a text message of {item['detail']['chars']} characters{edited}: too long for this call; "
                "the chat agent in this thread received it and answers there.")
    if item.get("kind") == "unsupported":
        return (f"- {handle} a {item['detail']['type']} message: you cannot use it in this call; the chat agent in "
                "this thread received it.")
    caption = f' with the caption "{item["caption"]}"' if item.get("caption") else ""
    parts = []
    for a in item.get("attachments") or []:
        if a["kind"] == "file" and "text" in a:
            clip = f" (first {len(a['text'])} of {a['chars']} characters)" if a.get("clipped") else ""
            parts.append(f"file {a['name']} ({_kib(a['bytes'])}){clip}, its text:\n{a['text']}")
        elif a["kind"] == "image":
            note = "too large to process" if a.get("reason") else f"{a['format'].upper()}, {_kib(a['bytes'])}"
            parts.append(f"a photo ({note}) that you cannot see in this call")
        elif a["kind"] == "unreadable":
            parts.append(f"an attachment that could not be read ({a['reason']})")
        else:
            parts.append(f"file {a.get('name', 'attachment')} ({_kib(a.get('bytes', 0))}) that you cannot read in "
                         f"this call: {a['reason']}")
    handed = "" if item.get("taken") else (" Nothing in it reached you except this note; the chat agent in this thread "
                                           "received it and answers there. Do not describe its contents.")
    return f"- {handle} the user sent {'; '.join(parts)}{caption}{edited}.{handed}"


def history_text(item):
    """What the conversation keeps: the same truthful line, marked as chat input, never fake model turns."""
    return "[Telegram chat input during the call] " + item_line("", item)[3:].strip()


__all__ = ["Thread", "ThreadInbox", "launch_thread", "take_event", "gateway_hook", "item_line", "history_text",
           "receipt", "describe_attachment"]
