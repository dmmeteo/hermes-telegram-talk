"""The profile gateway's side of an exact-thread Talk call, beside the inbound hook in thread/inbox.py.

`/talk` (a PTB CommandHandler wired through the public `register_telegram_handler`) answers in the very private
chat and topic the command arrived in with a web_app button. Its URL is the voice server's published Mini App URL
plus a fresh launch handle the inbox stores for (profile, bot, user, chat, topic); the voice server binds a call to
that thread only through the handle (thread.inbox.launch_thread).

`/talk_notify` shows a task user, in their private chat with the bot, the two switches that decide which voice tasks
get a Telegram status message (notify.py), as inline buttons; a tap switches one for that user only.

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

from .. import notify
from .inbox import ANALYSIS_CHARS, LOCK_WAIT_S, SESSION_STORE, Busy, Thread, ThreadInbox, _digest, _value

NOT_PRIVATE = "Talk opens only in a private chat with this bot."
NOTIFY_PRIVATE = "Task status settings open only in a private chat with this bot."
NOTIFY_DATA = "talk_notify:"


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


def _notify_account(home, bot_id, user_id):
    """(scope key, defaults) of a task user of this bot's voice server, else None (no answer to anyone else)."""
    published = ThreadInbox(home, time.time).published()
    if published is None or published.get("bot_id") != str(bot_id) or not isinstance(published.get("notify"), dict):
        return None
    if int(user_id) not in set(published.get("task_users") or ()):
        return None
    defaults = published["notify"]
    default = notify.Visibility(**{k: defaults.get(k) is True for k in notify.KINDS})
    return f"{published['profile']}:tg{bot_id}:{int(user_id)}", default


def notify_panel(home, bot_id, user_id, chat_type, chat_id):
    """(text, [(label, callback data)]) for `/talk_notify`; (None, None) answers nothing."""
    account = _notify_account(home, bot_id, user_id)
    if account is None:
        return None, None
    if chat_type != "private" or str(chat_id) != str(user_id):
        return NOTIFY_PRIVATE, None
    text, buttons = notify.panel(notify.load(home, *account))
    return text, [(label, NOTIFY_DATA + kind) for label, kind in buttons]


def notify_switch(home, bot_id, user_id, chat_type, chat_id, data):
    """A tap on one switch of the panel: stores it for this user and returns the new panel, else (None, None)."""
    account = _notify_account(home, bot_id, user_id)
    kind = data[len(NOTIFY_DATA):] if isinstance(data, str) and data.startswith(NOTIFY_DATA) else None
    if account is None or kind not in notify.KINDS or chat_type != "private" or str(chat_id) != str(user_id):
        return None, None
    notify.toggle(home, account[0], kind, account[1])
    return notify_panel(home, bot_id, user_id, chat_type, chat_id)


def telegram_handlers(native, adapter=None):
    """`register_telegram_handler` factory: `/talk` in a private chat or topic offers the Mini App bound to it;
    `/talk_notify` and its buttons switch which voice tasks get a status message."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    from telegram.ext import CallbackQueryHandler, CommandHandler

    def keyboard(buttons):
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)] for label, data in buttons])

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

    async def notify_command(update, context):
        message = update.effective_message
        chat, user = update.effective_chat, update.effective_user
        home = _home()
        if message is None or chat is None or user is None or user.is_bot or not home:
            return
        text, buttons = notify_panel(home, context.bot.id, user.id, chat.type, chat.id)
        if text is None:
            return
        thread_id = message.message_thread_id if getattr(message, "is_topic_message", False) else None
        await context.bot.send_message(chat_id=chat.id, text=text, message_thread_id=thread_id,
                                       reply_markup=keyboard(buttons) if buttons else None)

    async def notify_tap(update, context):
        query = update.callback_query
        home = _home()
        if query is None or query.from_user is None or query.message is None or not home:
            return
        chat = query.message.chat
        text, buttons = notify_switch(home, context.bot.id, query.from_user.id, chat.type, chat.id, query.data)
        await query.answer()
        if text is not None and buttons:
            await query.edit_message_text(text=text, reply_markup=keyboard(buttons))

    native.add_handler(CommandHandler("talk", talk))
    native.add_handler(CommandHandler("talk_notify", notify_command))
    native.add_handler(CallbackQueryHandler(notify_tap, pattern="^" + NOTIFY_DATA))


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
