# Hermes Talk (PoC v2)

Hermes Talk is a Telegram Mini App voice surface for one Hermes profile. Its menu label is "Talk".
It runs as an independent Hermes plugin next to the unchanged stock Telegram adapter. It makes no
core edits and uses no custom adapter. The experiment asks one thing: is hands-free voice with a
profile's own persona and conversation memory useful inside Telegram on a phone?

The browser is a thin audio client. The server owns everything else: identity, conversation
continuity, context and history. GPT-Live handles speech over WebRTC on the profile's ChatGPT
subscription.

## Scope

In scope:
- Signed-initData authentication, applied before any state, credential or upstream access.
- One persistent native Hermes session per verified Telegram user, configured profile and bot. The
  session is titled "Telegram Voice".
- Settled voice text stored in that session, with an explicit "new conversation".
- Voice instructions built from the profile SOUL, allowlisted skills, the conversation summary,
  recent turns and a small activity briefing.

Non-goals for this PoC:
- Starting or cancelling Hermes work from voice. The model is told to say it can't.
- Any paid API fallback, `/v1/realtime/sessions`, or raw audio storage.
- Posting voice turns as Telegram messages.
- Health or location data.
- Multi-profile activation, a global orchestrator, Discord thread automation, or Shopy.

## Shape

- `plugin.yaml` and `__init__.py` make up the plugin entrypoint. `register(ctx)` adds `hermes talk serve`,
  which runs `server.py` under the same Hermes interpreter and `HERMES_HOME`.
- `server.py` is a standard-library HTTP server. It serves `static/` (frontend-owned) and the fixed
  routes under `/tg-voice/`. Without `--config` it is still the original voice test: a single
  `api/session` route with the fixed test persona. With `--config` it serves the Talk API in
  [INTERFACE.md](INTERFACE.md), the frozen frontend contract.
- `hermes_talk/`:
  - `config.py` parses the YAML into frozen values.
  - `store.py` holds the account binding, voice-session ledger, idempotent turn ingestion, the end
    checkpoint and the read cursor.
  - `history.py` is Hermes `SessionDB` through its public methods, plus a synthetic in-memory twin.
  - `activity.py` builds the briefing.
  - `context.py` composes the instructions.
  - `talk.py` holds the runtime the routes call.
  - `fixtures.py` provides synthetic test data and a fixture server.
- The upstream exchange is adapted from Hermes Conduit (MIT, `LICENSES/CONDUIT-LICENSE`).

### Where data lives

Everything lives in the profile's own `state.db`, written through `SessionDB` methods. None of it is
written with SQL.

- **Turns.** Settled user and assistant text is appended with `append_message`. Each row carries the
  receipt `talk:<voice_session_id>:<event_id>` as its `platform_message_id`, so a retried batch is
  recognized with `has_platform_message_id`.
- **Binding and state.** The binding key `hermes_talk:binding:<profile>:tg<bot_id>:<user_id>` is kept
  with `get_meta`/`set_meta`. So is each conversation's state: voice sessions, summary and read
  cursor.
- **Never stored.** Audio, SDP, reasoning and tool output are never stored. Logs carry method, path,
  status and exception type only.

## How the voice context is built

The server builds the context for every call. Each part has its own character budget
(`context.budgets`), and the total has one too. A clipped section ends with `[clipped]`.

1. A fixed frame. It covers the profile name, the voice limits, and "nothing was executed" when
   asked to do work. It also says ending the call cancels nothing.
2. An optional language hint.
3. The profile SOUL, read through the host loader `agent.prompt_builder.load_soul_md` (which applies
   Hermes' own scan and truncation).
4. Skills named in `context.skills`, read through `tools.skills_tool.skill_view`. These are guidance
   text only and register no tools. Skills not on the list are never read.
5. The summary of earlier turns. This is the latest user requests, extracted at `api/end` with no
   model call.
6. The last `recent_turns` turns of this conversation.
7. Activity since the last visit. These are the profile's sessions from the configured sources that
   belong to this Telegram user. Sessions with no chat user (CLI, cron, kanban) are the operator's
   and are briefed only to `activity.owner_user_id`. Talk's own sessions and other users' sessions
   are excluded. Each item keeps its session id, source and time, plus a status. Unread items are
   listed oldest first up to `activity_items`. The rest are counted and offered next time, so a cut
   never skips one.
8. The greeting directive, if enabled. The model speaks first only after the call connects and
   mentions at most two updates.

The briefing uses four statuses:

| Status | Meaning |
| --- | --- |
| `failed` | A failed-turn row or an error end. |
| `active` | Open, with activity in the last 30 minutes. |
| `replied` | A stored assistant reply. Delivery to a chat is not proven. |
| `unknown` | Anything else, including stale open sessions. |

The read cursor moves only after a new assistant turn is stored for the call. A call that fails at
the provider leaves the briefing unread. An unended call expires after `voice.session_ttl_minutes`
and still gets its summary. A conversation whose native row was deleted is replaced on the next
bootstrap, and its old id answers 404.

Secret-shaped strings are redacted from every section. These are `sk-…` keys, bot tokens, JWTs and
`token=`/`password=` values.

Budgets are this application's character limits. The token window of the GPT-Live subscription
endpoint is not published. No setting here claims to match it, so raise budgets only after a live
qualification shows they're accepted.

## Configuration

See [talk.example.yaml](talk.example.yaml). It sets:
- profile id, display name and local avatar (URLs are refused);
- listen and speak colors;
- model, voice, language hint and call TTL;
- the greeting;
- the skill allowlist and budgets;
- activity window and sources;
- `privacy.persist_audio: false`, the only accepted value.

Typical changes:
- **Add a voice skill.** Put its name in `context.skills`.
- **Change the persona.** Edit the profile's SOUL. Talk reads it through the host on every call.
- **Change colors or the avatar.** Edit `appearance.*` or `profile.avatar`.
- **Change budgets.** Edit `context.budgets.*`.

## Run (manager, on the VPS)

```sh
# Install: copy or link this directory to $HERMES_HOME/plugins/hermes-talk and add it to plugins.enabled.
export HERMES_HOME=/home/me/.hermes/profiles/pewds
export TG_VOICE_BOT_TOKEN=...            # from the existing Pewds credential source; never on argv
export TG_VOICE_ALLOWED_USER_IDS=...     # numeric Telegram ids, comma separated
hermes talk serve --config /path/to/talk.yaml --origin https://her.dmmeteo.dev --port 8791
```

The reverse proxy maps `https://her.dmmeteo.dev/tg-voice/` to `127.0.0.1:8791/tg-voice/`. Talk needs
no webhook change, no polling and no gateway restart. The Mini App button, BotFather, the proxy and
deployment belong to the manager.

## Verify

```sh
python3 verify-backend.py --offline     # full backend gate (below)
python3 verify.py --offline             # legacy 26 signaling/security checks only
python3 verify.py --browser <out-dir>   # legacy UI/lifecycle run in a capped --network none container
python3 -m hermes_talk.fixtures         # synthetic Talk server for frontend work
```

`verify-backend.py --offline` runs three stages:

1. `verify.py --offline`. It must print `26/26`.
2. The Talk checks on synthetic history:
   - the frozen bootstrap shape with no private fields;
   - HMAC, bot, user, origin and field rejection on every route, with no state or upstream access;
   - SOUL, skill allowlist and redaction;
   - idempotent, out-of-order and invalid events;
   - foreign conversations and voice sessions;
   - reopen continuity and the summary;
   - explicit-new isolation;
   - clipping and config refusals;
   - activity scoping, status and cursor;
   - provider and credential failure;
   - expiry;
   - logs.
3. The same checks in a disposable `HERMES_HOME` under a real Hermes runtime. That run adds:
   - real `SessionDB`, `load_soul_md` and `skill_view`;
   - plugin discovery and registration through `PluginManager`;
   - a real `hermes talk serve` process answering bootstrap, allowlist and new, with the native
     session rows checked afterwards.

   The runtime comes from `HERMES_TALK_HERMES_PYTHON`, else `~/.hermes/installs/*/environments/*/venv`.
   If none exists, stage 3 prints `SKIPPED`. It never prints `PASS`.

## Status

| | Implemented | Tested offline | Host-integrated (disposable home) | Installed | Live |
| --- | --- | --- | --- | --- | --- |
| Auth, routes, contract | yes | yes | yes | no | no |
| Native session continuity, new, idempotent events | yes | yes | yes (real SessionDB) | no | no |
| SOUL / skills / summary / activity context | yes | yes | yes (host loaders) | no | no |
| Plugin registration, `hermes talk serve` | yes | n/a | yes | no | no |
| `api/session` against real GPT-Live | yes | fake upstream | not exercised | no | no |
| Frontend use of the v2 routes | frontend owner | no | no | no | no |

## Outcome checklist

- [x] Authenticated, account-bound persistent conversation; reopen resumes; explicit new is isolated
- [x] Settled text and bounded summaries in native Hermes history; no audio, SDP or tool output
- [x] Scoped SOUL, allowlisted skills and truthful activity briefing within explicit budgets
- [x] Frozen frontend contract (`INTERFACE.md`) and synthetic fixtures
- [x] Plugin registers and serves in a disposable Hermes home
- [ ] Real GPT-Live call with composed context on the VPS (manager)
- [ ] Frontend integration of bootstrap, events and end (frontend owner)
- [ ] Hands-free use inside Telegram on an iPhone (human verdict)

## Licenses

The project code is this repository's own. The signaling exchange is adapted from Hermes Conduit
(MIT); see `LICENSES/CONDUIT-LICENSE`. The Pulse-style briefing is an independent implementation
inspired by Conduit notifier's Pulse. No code was copied from it.
