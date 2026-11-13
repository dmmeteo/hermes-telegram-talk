# tg-talk + full-duplex = <3: a hermes-fullduplex core and a Telegram add-on

Status: proposed, 2026-11-09. Needs manager acceptance before any code change. It amends AGENTS.md (authentication,
plugin layout) and INTERFACE.md (a second launch proof), so neither applies until accepted. Deployment, the Traefik
route and plugin installs are operator actions.

## Context

Two products serve the same goal: hands-free voice with a Hermes profile that keeps working while the user talks.

- **Talk** (this repo, plugin `hermes-telegram-talk`) has Hermes as its brain: the profile's own SOUL,
  `SessionDB` history, native gateway tasks, Pulse, exact-thread chat input and task reports into the Telegram chat.
  It is the more stable and more capable of the two. It opens only as a Telegram Mini App.
- **Full Duplex** (`dmmeteo/full-duplex`, TypeScript, live `dd4a65d` at `x.dmmeteo.dev`) has its own brain (Pi
  Durable, own SOUL and SQLite) and reaches Hermes only through `consult`. Its strength is the desktop client. Its
  voice cycle (ROADMAP V01–V05) and real Core tasks (FDD-G002) are not qualified.

The user's direction (2026-11-08/09): Hermes stays the brain. Talk's engine becomes the product core, named
`hermes-fullduplex`, opened from the Hermes dashboard at `her.dmmeteo.dev`. `hermes-telegram-talk` becomes a thin
Telegram add-on on top of it. The TypeScript Full Duplex gives its interface ideas and retires.

## Decision

Two Hermes plugins, one voice server, one Hermes conversation per account, profile and bot.

| Plugin | Owns | Does not own |
| --- | --- | --- |
| `hermes-fullduplex` (core) | Voice server and `/talk/` routes, GPT-Live signaling, store, `SessionDB` history, context, tasks through the gateway, Pulse, reply voice, cookie sessions, the dashboard tab and its launch handle, the wide client with the sidebar | Anything Telegram |
| `hermes-telegram-talk` (add-on) | `initData` verification, `/talk` handles, chat input and chat-reply hooks (ADR 0004), bot avatar, task reports into the chat, the phone client inside the Mini App | State, history, tasks, signaling |

The add-on declares `requires_plugins: [hermes-fullduplex]`. Hermes then registers the core first
(`hermes_cli/plugins.py::resolve_plugin_load_order`). Both register in the voice server process. The core exposes
one small registry. A surface registers how it verifies a launch, which account the launch names, and where task
reports go. The core never imports Telegram code. Without the add-on, the core runs on the dashboard alone.

### Surfaces

| Surface | Launch proof | Client | Task progress |
| --- | --- | --- | --- |
| Dashboard | Dashboard launch handle (below) | Wide, sidebar with tasks (subagents), Pulse cards and conversation | In the sidebar |
| Telegram | Signed `initData` + `/talk` handle, unchanged | Phone, restyled, no subagent panel | Reports in the chat, as now |

Neither surface opens from a bare URL. A request without a valid claimed launch answers 401 before any state,
credential or upstream access, then the allowlist applies.

### Identity

Today every state key is a Telegram user id (store binding, `tasks.user_ids`, `activity.owner_user_id`). The core
renames that to an account id but keeps the values. The configured operator's account id stays the number it is now.
A dashboard handle names that account from config, so the dashboard and Telegram share one conversation, its tasks
and its Pulse cards. Stored keys (`Telegram Voice` title, `telegram-voice` source, `hermes_talk:*` metadata,
`talk:*` receipts) do not change. Existing conversations continue without a reset.

### Dashboard access without a second login

The Hermes dashboard mounts a plugin's `plugin_api.py` at `/api/plugins/<name>/` behind its own auth gate
(`hermes-model-viewer/dashboard` is a working example). The core's tab route is the bridge:

1. The route, already behind the dashboard login, writes a random one-time handle into the profile home, as `/talk`
   does in `thread_gateway.offer`. The handle names the configured operator account, profile and bot.
2. The tab frames `/talk/?dash=<handle>` with `allow="microphone; autoplay"`.
3. The voice server claims the handle once, within a short expiry, and replaces it with a cookie scoped to `/talk/`
   (HttpOnly, Secure, SameSite=Strict). Every mutation checks CSRF and the exact Origin, as in Full Duplex
   `docs/AUTH.md`.

Same origin means no third-party cookie problems inside the iframe. The core sends `frame-ancestors 'self'`. The
Traefik route strips the dashboard session cookie before the voice server, as the Unflat routes do.

### The voice reads the Telegram thread at call start

Today a bound call carries only chat input sent during the call (ADR 0004). The voice does not know what was said in
the thread before `/talk`. When a Telegram call binds a thread, the add-on contributes a bounded, clipped
`## This chat` section. It holds the latest turns of the Hermes session that the gateway's `SessionStore` maps to
that exact (chat, topic), read with `SessionDB.get_messages`. It is context, not history: nothing is copied into the
voice conversation, and no model turn is created. This amends ADR 0004 when implemented and must pass
`tests/e2e/run-thread.py --offline --require-host`.

## What moves where

| Today in this repo | Goes to |
| --- | --- |
| `server.py` routes, `talk.py`, `store.py`, `history.py`, `context.py`, `jobs.py`, `agent.py`, `gateway_rpc.py`, `activity.py`, `pulse*.py`, `spool.py`, `config.py` (core part) | core |
| `server.py::verify_init_data`, `thread_gateway.py`, `thread_inbox.py`, `avatar.py`, `receipts.py` (`TelegramSender`), Telegram config keys | add-on |
| `static/talk/` | Shared client: the core serves it. The add-on selects the phone layout without the sidebar |

From the TypeScript Full Duplex: the desktop look, device switch and replug recovery (FDD-B002) and the optional
greeting (FDD-F004) are ported into the client. Its engine, Master Session, own SOUL, sandbox tools, Codex
controller, password file and Drop consumer are dropped.

## Phases

Each phase ends at its own verification gate. Telegram behaviour stays correct throughout.

1. **Voice reads the thread.** `## This chat` in the current plugin, before any split.
2. **Split in place.** Core and add-on as two plugins with the surface registry. Gate: every existing verifier
   unchanged, plus the core starting without the add-on.
3. **Dashboard access.** Tab, handle, cookie session, `/talk/` path route on `her.dmmeteo.dev`.
4. **Clients.** Dashboard layout with the sidebar; Telegram restyle. Owned by the frontend owner.
5. **Retire the TypeScript Full Duplex.** After the user accepts the dashboard, `x.dmmeteo.dev` redirects or
   stops. Its SQLite stays a read-only archive.

## Open decisions

- **D1. Name collision.** `full-duplex` already names the TypeScript repo, its service, `~/.config/full-duplex`,
  `~/.local/share/full-duplex` and the FDD backlog. Until it retires, the new core needs distinct names for its
  repository, config and data (for example `hermes-fullduplex` everywhere, never bare `full-duplex`).
- **D2. Repositories.** This repo becomes the core and the add-on gets a new small repo, or both plugins stay in one
  repo. One repo keeps the cross-plugin tests (thread, integrated) in one place. That depends on Hermes installing
  a plugin from a subdirectory, which is unverified.
- **D3. Dashboard profile.** Which profile the tab opens (the dashboard runs on one Hermes home), or a switch.
- **D4. Typing on the dashboard.** A composer into the same conversation through the gateway (no new approval
  path), or voice only at first.
- **D5. Thread depth.** How many turns and characters `## This chat` carries, and whether tool output is excluded.
- **D6. Client stack.** Keep vanilla `static/talk/` (no build toolchain in a plugin) or adopt Svelte.

## Consequences

- AGENTS.md "signed initData + allowlist" becomes "a claimed launch (Telegram `initData` with a `/talk` handle, or a
  dashboard handle), then the allowlist". Profile and bot still come from config, never from the request.
- INTERFACE.md gains the handle claim and a cookie envelope. Every route currently requires `initData`, so this is
  not purely additive and needs the manager.
- The core's public registry becomes a second frozen contract, between the two plugins.
- One live voice stack to qualify instead of two. The TypeScript Full Duplex's open voice gates are not inherited.
