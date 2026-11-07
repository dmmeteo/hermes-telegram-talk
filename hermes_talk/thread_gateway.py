"""The profile gateway's side of an exact-thread Talk call, beside the inbound hook in thread_inbox.py.

`/talk` (a PTB CommandHandler wired through the public `register_telegram_handler`) answers in the very private
chat and topic the command arrived in with a web_app button. Its URL is the voice server's published Mini App URL
plus a fresh launch handle the inbox stores for (profile, bot, user, chat, topic); the voice server binds a call to
that thread only through the handle (thread_inbox.launch_thread).

`post_llm_call` sees each finished chat-agent turn. When that turn answered a photo or file a live call of the same
thread passed on to the chat agent (matched by the exact cached file the agent was given, and by the session's
origin in the gateway's own SessionStore), the reply is stored once as an attributed analysis item for that call.

The gateway hands `post_llm_call` the turn's clean persisted text as `user_message` (for a captioned photo that is
the caption alone: gateway/run_turn_prepare.py `_hmwa_apply_message_timestamp`, agent/turn_context.py), while the
native multimodal payload with its `[Image attached at: <cached path>]` hint exists only in `conversation_history`.
So the cached file is looked for in `user_message` and in this turn's own user row: the last user row of the
history, and only when it is the same input as `user_message`. Older rows are never scanned, so an earlier photo can
never be attributed to a newer, unrelated reply.
"""
import json
import os
import time

from .thread_inbox import ANALYSIS_CHARS, LOCK_WAIT_S, SESSION_STORE, Busy, Thread, ThreadInbox, _digest, _value

NOT_PRIVATE = "Talk opens only in a private chat with this bot."


def _home():
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME")


def offer(home, bot_id, user_id, chat_id, chat_type, thread_id, clock=time.time):
    """(text, url or None) for `/talk` from (bot, user, chat, topic); a url only for an allowed private chat."""
    inbox = ThreadInbox(home, clock)
    published = inbox.published()
    if published is None or published.get("bot_id") != str(bot_id):
        return None, None  # no voice server of this bot here: nothing to offer, and no answer to anyone
    if int(user_id) not in set(published.get("users") or ()):
        return None, None  # not a Talk user: no answer, nothing issued
    if chat_type != "private" or str(chat_id) != str(user_id):
        return NOT_PRIVATE, None
    thread = Thread(published["profile"], str(bot_id), int(user_id), str(chat_id),
                    str(thread_id) if thread_id else None)
    handle = inbox.issue_launch(thread)
    url = published["url"] + ("&" if "?" in published["url"] else "?") + "launch=" + handle
    where = "this chat" if thread.thread_id is None else "this topic"
    return f"Voice call for {where}. Photos, files and messages you send here during the call reach it.", url


def telegram_handlers(native, adapter=None):
    """`register_telegram_handler` factory: `/talk` in a private chat or topic offers the Mini App bound to it."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    from telegram.ext import CommandHandler

    async def talk(update, context):
        message = update.effective_message
        chat, user = update.effective_chat, update.effective_user
        if message is None or chat is None or user is None or user.is_bot:
            return
        home = _home()
        if not home:
            return
        thread_id = message.message_thread_id if getattr(message, "is_topic_message", False) else None
        text, url = offer(home, context.bot.id, user.id, chat.id, chat.type, thread_id)
        if text is None:
            return
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open Talk", web_app=WebAppInfo(url))]]) if url else None
        await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=markup,
                                       message_thread_id=thread_id)

    native.add_handler(CommandHandler("talk", talk))


def _user_text(message):
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "\n".join(p.get("text", "") for p in message if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _current_input(user_message, conversation_history):
    """This turn's input text: `user_message` plus, when it is this same input, the turn's own user row."""
    text = _user_text(user_message)
    rows = [m for m in conversation_history or () if isinstance(m, dict) and m.get("role") == "user"]
    if not rows:
        return text
    row = _user_text(rows[-1].get("content"))  # the turn's input; the loop appends only assistant/tool rows after it
    clean = text.strip()
    if row and (not clean or clean in row):  # a mismatch (e.g. a mid-turn note) adds nothing: miss, never misattribute
        return text + "\n" + row
    return text


def chat_reply(session_id=None, platform=None, user_message=None, assistant_response=None, turn_id=None,
               conversation_history=None, home=None, store=None, clock=time.time, **_):
    """`post_llm_call` body: the stored analysis item names, else []."""
    if platform != "telegram" or not isinstance(assistant_response, str) or not assistant_response.strip():
        return []
    home = home or _home()
    store = store or (SESSION_STORE[0] if SESSION_STORE else None)
    if not home or store is None or not session_id:
        return []
    inbox = ThreadInbox(home, clock)
    if not inbox.root.is_dir():
        return []
    entry = store.lookup_by_session_id(session_id)
    origin = getattr(entry, "origin", None)
    if origin is None or _value(origin, "platform") != "telegram":
        return []
    user_id, chat_id = str(getattr(origin, "user_id", "")), str(getattr(origin, "chat_id", ""))
    thread_id = getattr(origin, "thread_id", None)
    text = _current_input(user_message, conversation_history)
    stored = []
    for bind_path in inbox.root.glob("bind-*.json"):
        try:
            key = json.loads(bind_path.read_text(encoding="utf-8")).get("key")
        except (OSError, ValueError):
            continue
        if not isinstance(key, str) or bind_path.name != f"bind-{_digest(key)}.json":
            continue
        try:
            with inbox.locked(key, wait_s=LOCK_WAIT_S):
                stored += _attribute(inbox, key, user_id, chat_id, thread_id, text, assistant_response,
                                     session_id, turn_id, clock)
        except Busy:
            continue
    return stored


def _attribute(inbox, key, user_id, chat_id, thread_id, text, response, session_id, turn_id, clock):
    binding = inbox.binding(key, live_only=True)
    if (binding is None or str(binding.get("user_id")) != user_id or binding.get("chat_id") != chat_id
            or (binding.get("thread_id") or None) != (str(thread_id) if thread_id else None)):
        return []
    out = []
    folder = inbox._inbox(key)
    for path in sorted(folder.glob("m*")):
        if path.suffix not in (".json", ".done") or path.stem.endswith("-a"):
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (item.get("key") != key or item.get("taken") or item.get("conversation_id") != binding["conversation_id"]
                or not any(p and p in text for p in item.get("paths") or ())):
            continue
        kinds = {a.get("kind") for a in item.get("attachments") or ()}
        about = "photo" if kinds == {"image"} else "file" if kinds == {"file"} else "attachment"
        analysis = {"key": key, "kind": "analysis", "message_id": item["message_id"], "revision": item.get("revision"),
                    "at": clock(), "about": about, "text": response.strip()[:ANALYSIS_CHARS],
                    "clipped": len(response.strip()) > ANALYSIS_CHARS, "taken": True,
                    "conversation_id": binding["conversation_id"], "voice_session_id": binding["voice_session_id"],
                    "source": {"session_id": session_id, "turn_id": turn_id}}
        name = path.stem + "-a.json"
        if (folder / name).exists() or (folder / name).with_suffix(".done").exists():
            continue  # a repeated finalization of the same turn, or a later turn about the same file
        if inbox.offer(key, analysis):
            out.append(name)
    return out
