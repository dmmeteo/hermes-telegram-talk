# 0014 Status messages use Talk's own Bot API client, not Hermes' sender internals

Status: decided for fix/talk-sender-1116 (2026-11-16).

Task status messages were sent through `tools.send_message_tool._send_to_platform` and edited through `tools.send_message_senders._telegram_bot`. Both are private names of the Hermes core. Core commit 471537ae3a moved the out-of-process Telegram sender into the Telegram platform plugin (`plugins/platforms/telegram/standalone.py`) and removed `_telegram_bot` from `tools.send_message_senders`. After the production core moved to 3b42157858, every edit failed with ImportError. A Talk process that had started on the older core and first imported the sender after the update could not send either.

`TelegramSender` now makes the two Bot API calls itself over `urllib`: `sendMessage` once, then `editMessageText`. It imports nothing from Hermes or python-telegram-bot.

Options weighed:

- The platform registry (`platform_registry.get("telegram").standalone_sender_fn` after the public `discover_plugins`). This is the documented plugin contract and exists on both cores, but it only sends. No core at either revision has a public entry point for an edit, so the edit would still need a private Bot factory or a second client.
- A public core entry point for send and edit. None exists.
- Talk's own client. Chosen: one path for both calls, the same behavior on every core revision, and no dependence on which core modules a long-lived Talk process happens to hold.

What the user sees is unchanged. A status message is a status mark and a title with markup characters already removed (`receipts.MARKUP`), so Hermes' Markdown conversion only escaped it and Telegram displayed the same characters. Edits were always plain text. The message carries no parse mode, media or link-preview option, as before.

Kept from Hermes' sender, because a status message can meet them: `TELEGRAM_PROXY` is the request's proxy (HTTP or HTTPS proxies only; `urllib` has no SOCKS support, and such a value fails the request instead of going around the proxy), a forum's General topic (thread 1) is addressed as the chat itself, and a send to a deleted topic, which Telegram refuses, is repeated once to the chat itself.

Not kept: Hermes' fallback IP transport and its in-call retries. A first send that fails without proof that nothing was sent (5xx, a timeout, an unreadable reply) is uncertain and never resent, so a status message is never duplicated; a connection that failed before Telegram had the request is retried by `ReportDesk`, as are edits. Edits held by the 2026-11-16 sender breakage are released once on restart. Core 3b42157858 also sends silently unless `display.platforms.telegram.notifications` is `all`; Talk's client does not set `disable_notification`, which is what every core before that update did.

Result classification is unchanged in `receipts.classify` and `classify_edit`. Telegram's own descriptions now arrive unmodified ("Bad Request: chat not found"), so any 400 on a first send is a proven refusal. A refused connection or failed name lookup proves nothing was sent and is retried. A timeout, a lost reply or an unreadable answer stays uncertain and is never resent. An error names Telegram's description or the exception type, never the request URL, which contains the token.

Verification: `tests/reports/run.py` runs the sender over real sockets against a local fake Bot API and asserts that it loads no Hermes or python-telegram-bot module. `tests/logsecrets/run.py` searches the journal stream and stored records for the token on this path. `tests/e2e/run-agent.py` runs the status checks through `tests/agent/synthetic_telegram.py`, installed over the sender's one request function.
