# 0004 Talk continues the exact initiating Telegram thread: chat input, transcript and reply voice

Status: accepted for the local branch `feature/miniapp-thread-1107` (2026-11-07). Not deployed.

## Context

The user wants the Mini App to show only the live transcript and a reply-voice selector (plus the existing call
controls). Photos, files and text go to the bot in the Telegram thread that opened the call and must reach that
same call without reconnecting, without a second uploader or composer and without a global last-active-chat
fallback. Shopy, a second profile, gets the same behaviour with a female, restrained persona.

Interfaces inspected on the target core (`prime-tgt-cli-1103/target-sdk`, read-only):
- `hermes_cli/plugins.py` `VALID_HOOKS`: `pre_gateway_dispatch` runs "once per incoming MessageEvent, after the
  internal-event guard, BEFORE auth/pairing and dispatch" with `event, gateway, session_store`; `{"action": "skip"}`
  drops the event, `None` dispatches it normally (`gateway/run_inbound_hooks.py::_hm_pre_gateway_dispatch_hook`,
  called from `gateway/run_inbound.py`). Hermes keys its plugin manager by `HERMES_HOME`, so the hook runs in the
  profile's own gateway.
- `gateway/platforms/event.py` `MessageEvent` (text, `message_type`, `message_id`, `edit_revision`, `media_urls` as
  local cached paths, `media_types`, `raw_message` = PTB `Message`) and `gateway/session.py` `SessionSource`
  (`platform`, `chat_id`, `chat_type`, `user_id`, `thread_id`).
- `plugins/platforms/telegram/adapter.py`: `_build_message_event`, DM topics as `thread_id`, media cached through
  `gateway/platforms/base.py` (`cache_image_from_bytes_async`, `cache_media_bytes_async`) under the profile home.
- Telegram Mini App initData (signed by Telegram): `chat_type` and `chat_instance` only for some launches, the
  `chat` object only for attachment-menu launches, `start_param` for direct links. There is no topic id and no
  chat id for a menu-button launch.
- GPT-Live: Hermes' public docs (`website/docs/user-guide/features/voice-mode.md`) and `tools/voice_live.py`
  describe text context and delegation only, with the voice set at session creation (`audio.output.voice`). No
  public image input for the voice model was found.

## Decision

| Question | Decision | Why |
| --- | --- | --- |
| Which conversation is the call? | (profile, bot, user, chat, topic) from the signed launch: a launch inside the user's private chat with this bot (no `chat_type`, or `sender`) binds chat = user id; a Telegram-signed `start_param=topic<id>` names a topic of that chat. Groups, channels and other users' chats are never bound | Only these are proven by signed data; a browser field or the last active chat would be a guess |
| How does chat input reach the call? | The plugin's `pre_gateway_dispatch` hook in the profile's gateway takes only events whose bot (from the PTB message), chat, topic and sender equal a live binding file written by the voice server, writes them durably (fsync, create-once per message id and edit) and skips them; `api/context` carries them, `api/context/ack` stores them once (`talk:chat:` receipts) | A real registered producer and consumer, no polling of Telegram, no core edit, no new ingestion source |
| What can the voice use? | Text and whole UTF-8 text files up to 64 KB (6000 characters each). Everything else (photos, PDFs, voice notes, oversized or undecodable files) is noted truthfully in the call and passed on to the profile's normal chat agent in the same thread | The voice model has no verified image input; the chat agent's vision path is real. A note never claims the model saw bytes |
| Minimize, restore, retry | Pending input belongs to the conversation and exact thread, not one voice session; it is offered until acked and flushed into history at `api/end` | No loss, no double injection, no mixing of threads |
| Reply voice | Operator-listed `voice.choices`, persisted per (profile, bot, user) through `api/voice`; applied when a session is created. A live change asks for an explicit Reconnect that keeps the conversation | The provider sets the voice at session creation; hot switching is not claimed |
| Shopy | `talk.shopy.example.yaml`: `marin`, `persona.self_reference: feminine`, short polite greetings; Pewds' config unchanged | Per-profile isolation already exists; only source examples, no live profile access |

## Consequences

- The hook needs in-process plugin isolation (the default). Under `plugins.isolation` host mode the event's PTB
  message is not available, the bot is unproven and nothing is taken (messages flow normally).
- After a Talk server restart, a still-live binding keeps taking input until its call ends or expires; that input
  is delivered by the next call of the same thread and conversation.
- A Mini App opened from the menu inside a DM topic binds the main chat lane (Telegram does not sign the topic);
  only a `topic<id>` direct link binds a topic. Real-phone behaviour, background audio and the provider's
  acceptance of `marin` are not qualified here.
