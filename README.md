# Hermes Telegram Talk (PoC v2)

Hermes Telegram Talk is a Telegram Mini App voice surface for one Hermes profile. Its menu label is "Talk".
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
- `server.py` is a standard-library HTTP server for the fixed routes under `/tg-voice/`. Without
  `--config` it is still the original voice test: the `static/` page and a single `api/session` route
  with the fixed test persona. With `--config` it serves the Talk UI from `static/talk/` (avatar,
  energy ring and Mute; see [DESIGN.md](DESIGN.md)) and the Talk API in [INTERFACE.md](INTERFACE.md),
  the frozen frontend contract. `static/` and `static/talk/` are frontend-owned.
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

The plugin is named `hermes-telegram-talk`. The Python package `hermes_talk`, the `talk serve`
command, `/tg-voice/` routes, and menu label `Talk` retain their existing names. The stored
`Telegram Voice` title, `telegram-voice` source, `hermes_talk:*` metadata keys, account binding,
`talk:*` receipts, conversation summaries, and activity cursors remain compatible. Renaming the
installed plugin does not create a new conversation or reset its read cursor.

The Talk HTML uses content-qualified `app.js?v=...` and `app.css?v=...` URLs. Their values are the
first 16 hex characters of each asset's SHA256. Update them when either asset changes. The offline
frontend check verifies these values. This separates the current UI from legacy assets already
cached under the unversioned URLs, including caches that override the origin's `no-cache` header.

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
   listed oldest first by (last time, session id), up to `activity_items`. Only lines the final
   instructions carry whole count as read. A line omitted for lack of room, clipped, or past the
   item limit is counted and offered again next time. The reader pages through
   `list_sessions_rich` with overlapping pages, up to 1000 sessions per visit. If more changed, the
   briefing says older updates were not read, rather than claiming it saw everything.
8. The greeting directive, if enabled. The model speaks first only after the call connects and
   mentions at most two updates.

The briefing uses four statuses:

| Status | Meaning |
| --- | --- |
| `failed` | A failed-turn row or an error end. |
| `active` | Open, with activity in the last 30 minutes. |
| `replied` | A stored assistant reply. Delivery to a chat is not proven. |
| `unknown` | Anything else, including stale open sessions. |

The read cursor is the (last time, session id) key of the last complete item in that call's
instructions. It moves only after a new assistant turn is stored for the call. A call that fails at
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
# Install: copy plugin.yaml, __init__.py, server.py, hermes_talk/, static/ and LICENSES/ to
# $HERMES_HOME/plugins/hermes-telegram-talk and add hermes-telegram-talk to plugins.enabled (see "Native install" below).
export HERMES_HOME=/home/me/.hermes/profiles/pewds
export TG_VOICE_BOT_TOKEN=...            # from the existing Pewds credential source; never on argv
export TG_VOICE_ALLOWED_USER_IDS=...     # numeric Telegram ids, comma separated
hermes talk serve --config /path/to/talk.yaml --origin https://her.dmmeteo.dev --port 8791
```

The reverse proxy maps `https://her.dmmeteo.dev/tg-voice/` to `127.0.0.1:8791/tg-voice/`. Talk needs
no webhook change, no polling and no gateway restart. The Mini App button, BotFather, the proxy and
deployment belong to the manager.

### Native install

`hermes -p <profile> talk serve` resolves the profile's `HERMES_HOME` and runs `server.py` from that
profile's installed plugin directory; the gate checks both on the running process. In a fresh HOME,
`hermes plugins install file://<checkout> --ref <sha> --enable` clones and enables the plugin, but it
and `hermes plugins enable` first provision a separate Hermes runtime and install Python dependencies
(about 240-700 MB, with downloads). The offline gate therefore installs the plugin as an immutable,
read-only directory copy and enables it with the `plugins.enabled` key those commands write, then
confirms it with `hermes plugins list`. Real-profile installation and activation remain the manager's.

## Verify

```sh
python3 verify-integrated.py --offline --require-host   # canonical gate: backend + frontend offline, host mandatory
python3 verify-integrated.py --browser <out-dir>        # legacy and Talk Chromium inventories, capped containers
python3 verify-frontend.py --mobile <out-dir>            # Chromium and actual WebKit: cold/warm assets, rotation, retry and native audio unlock
python3 verify-backend.py --offline     # full backend gate (below)
python3 verify-frontend.py --offline    # legacy 26 + frontend unit checks + HTTP contract on this backend
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
     session rows checked afterwards;
   - the plugin as a read-only directory install in a named profile of a disposable HOME, served by
     `hermes -p synthetic talk serve` from outside this checkout: the process runs the installed
     `server.py` with that profile's `HERMES_HOME`, serves the `static/talk/` bytes, refuses a foreign
     Origin, a bad signature and a non-allowlisted user, and provisions no runtime.

   Both stage 2 and 3 also check that configured mode serves `static/talk/` and no-config mode keeps
   the original page and its single route.

   The runtime comes from `HERMES_TALK_HERMES_PYTHON`, else `~/.hermes/installs/*/environments/*/venv`.
   If none exists, stage 3 prints `SKIPPED`. It never prints `PASS`. `verify-integrated.py
   --require-host` (and `HERMES_TALK_REQUIRE_HOST=1`) turn that skip into a failure.

Prerequisite runtimes: Python 3 for the gate, a Hermes install for stage 3, Docker for the browser
inventories (`django-admin-repl-verify-browser:pw1.63.0`, Playwright 1.63 with Chromium, overridable as
`TALK_BROWSER_IMAGE`) and Node for the frontend unit checks (from `PATH`, else the local
`node:22.22.3-bookworm-slim` image as `TALK_NODE_IMAGE`). Containers run with `--network none`.
The mobile gate also requires the matching WebKit binary. `TALK_TEST_AVATAR=/absolute/public-avatar.jpg`
optionally supplies a public test image through a read-only file mount. Otherwise the fixture uses
the package's SVG avatar. All identities and text remain synthetic.

## Status

| | Implemented | Tested offline | Host-integrated (disposable home) | Installed | Live |
| --- | --- | --- | --- | --- | --- |
| Auth, routes, contract | yes | yes | yes | no | no |
| Native session continuity, new, idempotent events | yes | yes | yes (real SessionDB) | no | no |
| SOUL / skills / summary / activity context | yes | yes | yes (host loaders) | no | no |
| Plugin registration, `hermes talk serve` | yes | n/a | yes | in fixture (named disposable profile) | no |
| Talk UI served in configured mode; legacy page without config | yes | yes | yes | in fixture | no |
| Frontend use of the v2 routes (Chromium, local WebRTC peers) | yes | yes | n/a | n/a | no |
| `api/session` against real GPT-Live | yes | fake upstream | not exercised | no | no |
| Voice task execution | no (unsupported; the model says so) | n/a | n/a | n/a | n/a |

## Outcome checklist

- [x] Authenticated, account-bound persistent conversation; reopen resumes; explicit new is isolated
- [x] Settled text and bounded summaries in native Hermes history; no audio, SDP or tool output
- [x] Scoped SOUL, allowlisted skills and truthful activity briefing within explicit budgets
- [x] Frozen frontend contract (`INTERFACE.md`) and synthetic fixtures
- [x] Plugin registers and serves in a disposable Hermes home
- [ ] Real GPT-Live call with composed context on the VPS (manager)
- [x] Frontend integration of bootstrap, events and end, served by this plugin (fixture and Chromium)
- [ ] Hands-free use inside Telegram on an iPhone (human verdict)

Known limit: the client sends settled text in chunks of the default `event_text_chars` (2000). A
config with a lower `context.budgets.event_text_chars` would refuse those chunks; keep the default for
this client.

## Licenses

The project code is this repository's own. The signaling exchange is adapted from Hermes Conduit
(MIT); see `LICENSES/CONDUIT-LICENSE`. The Pulse-style briefing is an independent implementation
inspired by Conduit notifier's Pulse. No code was copied from it.
