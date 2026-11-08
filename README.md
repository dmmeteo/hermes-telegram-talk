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

- Optional voice tasks: a GPT-Live delegation becomes a real Hermes task in the profile's own agent, and its
  result comes back on that delegation (see "Voice tasks"). Off unless `tasks.enabled` is set.

Non-goals for this PoC:
- Approving flagged actions from voice. A task that needs approval is blocked and says so.
- Any paid API fallback, `/v1/realtime/sessions`, or raw audio storage.
- Posting voice turns as Telegram messages.
- Health or location data.
- Multi-profile activation, a global orchestrator or Discord thread automation. Shopy has a source example
  (`talk.shopy.example.yaml`); installing it on the live profile is the manager's.
- A Mini App uploader or text composer: photos, files and text go to the bot in the Telegram chat itself.

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
  - `store.py` holds the account binding, voice-session ledger, idempotent turn ingestion and the end
    checkpoint.
  - `history.py` is Hermes `SessionDB` through its public methods, plus a synthetic in-memory twin.
  - `activity.py` scopes the profile's sessions to the user and gives each a truthful status.
  - `pulse.py` is the Pulse engine's decisions: cards, fingerprints, fallback importance, item attribution and
    feedback parsing. `pulse_ledger.py` is its persistent per-account record (see "Pulse").
  - `pulse_select.py`, `pulse_memory.py` and `pulse_followup.py` are the model steps ported from Conduit Pulse
    (selection, memory extraction, follow-up checks); `pulse_engine.py` runs them and `pulse_model.py` is their
    only model route, the plugin's host-owned `ctx.llm`.
  - `context.py` composes the instructions and mid-call context updates.
  - `jobs.py` is the task record, its state machine and the job board (dedupe, scheduling, receipts, restart).
  - `agent.py` runs a task as one native `hermes chat` process and decides task readiness.
  - `talk.py` holds the runtime the routes call.
  - `fixtures.py` provides synthetic test data and a fixture server.
- The upstream exchange is adapted from Hermes Conduit (MIT, `LICENSES/CONDUIT-LICENSE`).

### Where data lives

The plugin is named `hermes-telegram-talk`. The Python package `hermes_talk`, the `talk serve`
command, `/tg-voice/` routes, and menu label `Talk` retain their existing names. The stored
`Telegram Voice` title, `telegram-voice` source, `hermes_talk:*` metadata keys, account binding,
`talk:*` receipts, conversation summaries, and activity cursors remain compatible. Renaming the
installed plugin does not create a new conversation or reset its read cursor. A legacy activity cursor is where an
account's first Pulse scan starts; afterwards each card carries its own read state.

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
  with `get_meta`/`set_meta`. So is each conversation's state (voice sessions and summary) and each account's
  Pulse record, `hermes_talk:pulse:<profile>:tg<bot_id>:<user_id>`.
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
7. Activity since the last visit: the Pulse cards selected for this call (see "Pulse"). Sessions come from the
   configured sources and belong to this Telegram user. Sessions with no chat user (CLI, cron, kanban) are the
   operator's and are briefed only to `activity.owner_user_id`. Talk's own sessions and other users' sessions are
   excluded. Each line keeps its call label (`P1`, `P2`...), status, session id, source and time. Up to
   `activity_items` lines go in, most important first; a line that does not fit whole is left out and counted,
   never clipped. The reader pages through `list_sessions_rich` with overlapping pages, up to 1000 sessions per
   visit. If more changed, the briefing says older updates were not read, rather than claiming it saw everything.
   When room allows, a short "Pulse updates" section tells the model to name updates by title and how to delegate
   feedback.
8. The greeting directive, if enabled. The model speaks first only after the call connects and
   mentions at most two updates. With `greeting.phrases` configured, the call instead opens with exactly one of
   those lines (chosen locally, never the account's previous one, no model request) and leaves updates for later.

### Fast start and in-call catch-up

Opening a call never scans the profile's sessions or asks the Pulse model. `api/session` reads the account's
prepared Pulse state (the cards and selection of the last background run; when there is none yet, the briefing
says activity is still being read and must not be guessed) and starts one background preparation per account:
scan, then model selection. Its result reaches the same live call through the existing `api/context` refresh
(revisioned, acknowledged, deduplicated); being supplied to the model is exposure, never hearing. The client
refreshes context after connecting, then every few polls, also when tasks are off.

Freshness sources: the call start (above), call end (`engine.poke`), the engine's slow tick and in-call context
refreshes. Host session events are not subscribed: the plugin registers only its CLI command and the Talk server
runs in its own process, so session changes elsewhere in the profile are picked up by these refreshes, not pushed.

The client's 30 s connection deadline ends when transport and audio are usable (peer connected, data channel open,
handshake, audio running and playing), not when the greeting finishes. A greeting that is not heard or not finished
within 12 s falls back to listening instead of ending the call. Each call records content-free stage timings and,
on failure, the first unmet gate (`connect:session`, `connect:peer`, `connect:handshake`, `connect:audio`, ...);
`api/end` carries them to the server log.

The briefing uses four statuses:

| Status | Meaning |
| --- | --- |
| `failed` | A failed-turn row or an error end. |
| `active` | Open, with activity in the last 30 minutes. |
| `replied` | A stored assistant reply. Delivery to a chat is not proven. |
| `unknown` | Anything else, including stale open sessions. |

Being in the instructions is not being heard. An update stays unread until the call itself shows it was told and
answered (see "Pulse"). A call that fails at the provider exposes nothing. An unended call expires after
`voice.session_ttl_minutes` and still gets its summary. A conversation whose native row was deleted is replaced on
the next bootstrap, and its old id answers 404.

## Pulse

Pulse is Talk's briefing engine, ported for voice from Hermes Conduit notifier's Pulse at d25391f6 (MIT,
`LICENSES/CONDUIT-NOTIFIER-LICENSE`; prompts and mechanisms ported and adapted; module map in
[docs/adr/0002-pulse-engine-model-route.md](docs/adr/0002-pulse-engine-model-route.md), evidence model in
[docs/adr/0001-pulse-per-item-evidence.md](docs/adr/0001-pulse-per-item-evidence.md)). One record per account
(profile, bot, Telegram user) survives reopen, restart and `api/new`, and no other account can read or move it.

- **Model route.** Every model step calls the plugin's own `ctx.llm` (Hermes' host-owned plugin LLM facade on the
  profile's active model and auth, no override, no new account or billing). `register(ctx)` binds it and the voice
  server's Pulse worker loads the profile's plugins once, 30 s after the server started answering, so the binding
  exists there (call setup until then uses the fallback). Without it each step degrades and
  records so: selection `fallback` with the reason, memory `unavailable`, follow-up checks retried.
- **Candidates and cards.** Each scanned session revision becomes a card fingerprinted by (kind, session,
  status, normalized title). A changed status supersedes the unread older card of the same session. Follow-ups
  that a check decided to tell become cards too.
- **Selection.** The model picks the briefing (`pulse_select.py`, upstream `pulse_candidates` prompt): per
  candidate bring_up, value 1-5, a one-word skip (done, covered, raised, rated, minor), a line and a why; it sees
  what Pulse remembers, what it brought up lately and lessons from the user's feedback. Picks go by value up to
  `activity_items`; the why is carried on the card's line. The selection is cached against the eligible cards'
  fingerprints and refreshed by the engine; call setup refreshes it (at most 8 s) only when the cards changed.
  Fallback only: status base (failed 0.9, replied 0.6, active 0.5, unknown 0.3), follow-up and unconfirmed boosts,
  unheard-exposure fatigue and learned preference.
- **Memory.** After a call ends (and every 5 min), the engine reads new lines of the account's Talk conversation
  and of the profile sessions already scoped to the account. The model extracts commitments, promises,
  intentions, interests, facts and watches (`pulse_memory.py`, upstream prompt). An item is kept only if its quoted
  evidence is on a line said by the right speaker; a due date only if its words were said. Items keep provenance
  (session, source, line, evidence), repeats refresh them, `replaces` retires the old one, `closed` resolves them.
  Interests, intentions and facts decay by half-life unless said again. Feedback still updates per-source counts
  and topic weights, and a told list serves deduplication.
- **Follow-up checks.** Open promises, commitments and watches get model checks (`pulse_followup.py`, upstream
  prompts): tell, done, drop or wait. First check after 30 min (promise, watch), one day or the due day's 09:00
  (commitment); then 2 h, 6 h, 1 d, 2 d, 3 d, shortened by the model's hint; a failed call retries in 30 min
  without counting; give up after 8 checks or 7 days (watch 30). "Tell" becomes a follow-up card: acknowledged in a
  call it marks the item reminded, `dismiss` cancels the item. Pulse's own spoken follow-up is never re-extracted.
- **Evidence per card, per call.** `candidate` (known), `exposed` (the voice model received it whole, in the
  instructions or an acknowledged context update), `mentioned` (a settled assistant turn of that call named it and
  was not spoken over), `acknowledged` (a settled user turn followed that mention in the same call, or feedback).
  Neither a caption nor audio output proves the user heard anything, so a mention alone never closes a card: a
  call that ends after one offers it again, marked "mentioned before, not confirmed heard". A greeting names no
  card and closes none. The client marks an assistant turn the user started speaking over as `interrupted`; it is
  recorded and is not evidence.
- **Feedback.** The model delegates `pulse P<n> useful|not useful|dismiss|later|more` (`api/pulse/feedback`),
  optionally with a reason (`not relevant|already knew|too late|too often|wrong time`).
  Useful and more acknowledge the card (more also speaks its details), not useful and dismiss close it, later
  snoozes it for three hours as a follow-up. A resubmitted delegation returns its first outcome.
- **Learned preferences.** Each rating is a signal (`pulse_policy.signal`): useful and not useful/dismiss are
  thumbs, more is a Do it at the sent stage, later leaves none. `pulse_prefs.py` is upstream's model unchanged
  (regularized logistic regression over kind, place and timeliness, 60-day long memory, a place following its last
  three votes): a pick's score (learning + value + pick) against `SURFACE_AT`/`PUSH_AT` sets its level. Reasons
  leave lessons (`pulse_policy.reason`): too often steps the kind's ceiling one level quieter, too late makes it
  count as due sooner, already knew is told to the selection model, wrong time closes that part of the day for
  its leads, not relevant turns off what comes from that place.
- **Delivery policy.** `pulse_policy.py` (upstream levels, quiet, hours and brief): push leads the call's
  briefing ("lead with this"), screen is carried after the leads, brief is held for the first call of the next
  local day that is not in the night or a quiet spell (stamped once per day), off is not brought up. Optional
  config `pulse: {zone, night: ["22:00", "07:00"], quiet_days: [5, 6], quiet_until: <epoch>, nudge}`; with no
  night configured none applies, because a Talk call is always opened by the user. Decisions are written only
  when a card's level changes.
- **Claims.** The memory reading also lists what Hermes said it had just done with a tool (reminder, event, job,
  message). `pulse_claims.py` judges each against the tool calls stored in that turn of the same session
  (SessionDB `get_messages`, public): a worked call that could do it backs it; an unbacked claim becomes a
  `[claim]` card (screen by default) that the selection model may bring up.
- **Snooze and watch.** A snoozed card comes back once due, boosted and labelled. An acknowledged `active` card
  is watched; its next status arrives labelled "follow-up on an earlier update".
- **Deduplication.** An unchanged session is never told twice. The same news (status and normalized title) from
  another session within ten minutes of an unread or told card is recorded as a duplicate and not offered.

To change a typical behavior: a prompt rule or skip reason in `pulse_select.py`/`pulse_memory.py`/
`pulse_followup.py` (teach `tests/pulse/model_fixture.py` the new answer and add a step to
`verify-pulse-engine.py`); a feedback kind or the evidence rules in `pulse.py` (a case in `verify-pulse.py`);
storage shape in `pulse_ledger.py`; when the engine runs in `pulse_engine.py`; levels, reasons and quiet/night
rules in `pulse_policy.py`; claim judging in `pulse_claims.py` (a step in `verify-pulse-engine.py` for each). What
was ported, adapted or left out, and why, is in
[docs/adr/0003-pulse-whole-engine.md](docs/adr/0003-pulse-whole-engine.md).

Secret-shaped strings are redacted from every section. These are `sk-…` keys, bot tokens, JWTs and
`token=`/`password=` values.

Budgets are this application's character limits. The token window of the GPT-Live subscription
endpoint is not published. No setting here claims to match it, so raise budgets only after a live
qualification shows they're accepted.

## Chat input from the Telegram thread

A call is the voice of the Telegram conversation that opened it, not a separate chat. The Mini App shows the live
transcript above a compact bottom row. The three-dot menu contains the reply-voice selector and End call. Everything else goes to the bot in the chat:
minimize the Mini App, send a text, a file or a photo, come back. Decisions and the inspected interfaces are in
[docs/adr/0004-exact-thread-chat-input.md](docs/adr/0004-exact-thread-chat-input.md).

- **Which chat.** Send `/talk` in the private chat (or a topic of it) with the bot. The plugin's Telegram handler
  (`register_telegram_handler`) answers in that same chat and topic with an "Open Talk" web_app button. Its URL is the
  Mini App URL the voice server published plus a fresh launch handle that the gateway stored for (profile, bot,
  user, chat, topic). `api/session` binds the call to that thread only through the handle: it must name this
  profile, bot and signed user, be opened within 10 minutes, and it is claimed by one Telegram launch (a reconnect
  of the same page keeps it; another launch gets "already used"). A Main Mini App link may carry the same handle
  as `startapp`. A menu launch without a handle, a group, a channel or another account's handle binds nothing and
  the Mini App says so ("Not linked to a chat. Send /talk there to link it."). There is no fallback to the main
  chat or a last active chat, and the browser cannot name one. Nothing is sent or registered with Telegram until
  an operator restarts the profile's gateway with this plugin.
- **How it arrives.** `register(ctx)` adds a `pre_gateway_dispatch` hook. In the profile's gateway it sees each inbound
  message before Hermes' own dispatch and takes it only when the bot (from the Telegram message), chat, topic and
  sender equal a live binding (`plugin-data/hermes-talk/threads/`). Taken input is written once per message id and edit
  (fsync) before the hook skips it; if writing fails, the message goes to Hermes as usual. Commands (`/stop`, ...)
  are never taken.
- **What the voice gets.** The next in-call context refresh carries a `## Chat input` section (no reconnect, no
  Pulse wait). Text and UTF-8 text files up to 64 KB (6000 characters each) are given whole. Photos, PDFs, voice
  notes and files that are too large or not UTF-8 are not given to the voice: the call gets a note saying so and
  the message continues to the profile's normal chat agent in the same thread, which gets the real multimodal
  payload (or Hermes' vision description, per the profile's image routing). When that agent's turn finishes, the
  plugin's `post_llm_call` hook matches the turn to the exact cached file it was given and to the session's origin
  in the gateway's own SessionStore, and stores the reply (up to 1500 characters, once per message) for the same
  live call and conversation. The voice then hears it as the chat agent's reply about that photo or file ("You did
  not see it yourself"). Replies for another thread, an ended or rebound call, or an unrelated turn are not
  carried. For a captioned photo the gateway passes the hook only the caption as `user_message`; the cached file is
  then found in the turn's own (last) user row of `conversation_history`, used only when it holds that same caption,
  so an older photo in the history is never attributed to a newer reply. The voice model itself has no verified
  image input and is never told it saw anything.
- **Never stalls the gateway.** The gateway waits at most 0.5 s for a thread's lock (the voice server holds it while
  acking or flushing to SessionDB). After that the message goes to Hermes as usual, so it is answered in the chat
  and never lost; only that one message misses the call.
- **Exactly once.** An input is stored in the conversation (SessionDB, receipt `talk:chat:...`) when its context
  update is acked, or at `api/end` if the call never got it. A minimized page that reconnects without `api/end`
  still gets the earlier call's undelivered input, once. Edits are separate inputs, marked as edited.
- **Transcript.** Display only, in this page: interim speech is replaced by the final text, interrupted replies are
  marked, nothing is posted to Telegram. Task reports keep their own routing.

### Reply voice

`voice.choices` labels voices from the complete nine-entry native subscription catalogue, with the profile default first. `voice.voice` and every choice must be subscription voices. The signaling URL answers the offer for any voice, then GPT-Live refuses a call whose voice the ChatGPT account lacks, so ICE never connects. API-key voices such as `marin` fail config load. The three-dot menu contains the selector. `api/voice` stores the choice per
(profile, bot, user) in SessionDB meta. The provider sets the voice when a session is created, so the choice applies
from the next connection; during a call the page offers an explicit Reconnect that keeps the conversation.

Bot avatars refresh through authenticated same-origin image responses, using a bounded per-bot cache and local fallback. See [the compact UI and catalogue decision](docs/adr/0005-compact-talk-media-and-avatar.md) for provenance and qualification limits.

### Shopy

`talk.shopy.example.yaml` is a source example for the Shopy profile: `sol` (a female-presenting subscription voice),
`persona.self_reference: feminine` (Ukrainian feminine self-reference) and short, polite greetings. Pewds keeps its own
config and phrases. Shopy previously used the API-key voice `marin`, and every Shopy call stopped before the peer
connected (see [ADR 0005](docs/adr/0005-compact-talk-media-and-avatar.md)).

## Voice tasks

When `tasks.enabled` is true, the voice model's client delegations become Hermes tasks:

1. GPT-Live sends `delegation.created` (`item.type` delegation, `target` client, text in `item.content[].text`) on the
   data channel. `static/talk/app.js` posts it to `api/task` with the original `delegation_item_id`, and appends the
   returned `reply` to that delegation on the `commentary` channel ("working on it; the result will follow").
2. The server checks initData, Origin, the allowlist, `tasks.user_ids`, the account's live call, limits and readiness.
   It stores a job keyed by account scope, call generation and delegation id. A repeated delegation returns the same
   job and starts nothing.
3. The job board starts `hermes chat --query-file - --format stream-json --source telegram-voice --max-turns N
   --run-budget S -t <tasks.toolsets> [--resume <session>]` under the profile's `HERMES_HOME`, in `tasks.workdir`. The
   prompt carries the bounded voice turns before the request and then the request once. That is the profile's own
   agent with its own model, provider, skills, memory and approval policy. Tasks of one conversation run one at a
   time in one native session (`--resume`), so the agent keeps its own context across tasks, calls and restarts.
   The child environment drops the Talk bot token, the allowlist and every Hermes approval bypass variable.
4. The browser polls `api/tasks`. When the job ends it appends the server's `spoken` text to the original delegation
   on `speakable`, in 500-byte chunks (Conduit framing), once per call, and then acks with `api/task/ack`.
5. If the call ended first, the task keeps running. The next call's instructions list it ("running" or its result).
   A result that arrives during a later call goes out as a `session.context.append` from `api/context`, and only its
   ack (`api/context/ack`) marks it delivered. A result carried in instructions counts as delivered once an
   uninterrupted assistant turn of that call names its task ("task 3"); a greeting does not. The same update carries
   new Pulse cards, which its ack marks exposed, not heard.

Job states are accepted, running, succeeded, failed, cancelled and interrupted. A running job is stored before its
process starts. A job whose process could not start fails at once and takes no slot, so queued jobs behind it still
start. One Talk server serves a profile: `server.py` binds its port and takes `plugin-data/hermes-talk/serve.lock`
before recovering anything, and a second one exits. After a Talk restart a live process is adopted and finishes normally. A dead one is settled from its
output record, or marked interrupted and never rerun.

The native process runs behind `hermes_talk/spool.py`, which owns its stdout pipe and keeps only the session event
and the final result (text up to 1 MiB, marked when cut) in the job's output record. The record stays bounded however
much the run prints; files the run writes are not limited. The voice gets the result clipped to `tasks.result_chars`.
A result too long for a context update goes in as a short synopsis that says to delegate "result of task N", which
returns the stored result without running anything. Unread results are offered oldest first, whatever their age. Receipt is separate: delivered means the client appended the
result (or the call that carried it spoke), not that anyone heard it. Ending a call never cancels a task; `api/task/
cancel` and a spoken "cancel task N" delegation do.

Readiness is checked on every bootstrap and task. Tasks are unavailable, and the voice is told it cannot start work,
unless the profile has a model, every `tasks.toolsets` name exists, the Hermes command is installed, approvals are
`manual` or `smart`, and `approvals.single_query_mode` is not approving. In single-query mode Hermes itself blocks every
flagged action with nobody to approve it, before any smart classifier runs, so `smart` behaves like `manual` for tasks;
`off` and unrecognised modes are refused. Talk adds no approval path and never passes `--yolo`. Full tool parity with an
attended chat is not claimed: anything that needs approval fails and the result says so.

### Task reports in Telegram

With tasks on, every finished task also sends a short report to the private chat between the user and the Talk bot
whose signed initData opened the call, even when the call has ended. The report says what happened, the result or the
reason and nothing else: no transcript, prompt, tool log or voice turns, and secret-shaped text is redacted. A result
longer than `receipts.max_chars` goes as a cover message with the full result attached as a Markdown document from
the profile's `plugin-data/hermes-talk/reports/`. The task prompt asks the agent to answer in the conversation's
language; the fixed framing words are English.

The user can send a report elsewhere only by naming a destination the operator listed in `receipts.destinations` for
that user (`REPORT TO: <name>` at the end of the delegation). It then goes only there. The browser cannot pass a chat
id; any other name, a raw chat id or "home" is refused before the task starts. A bare platform target (a home channel)
is refused in the config.

Sending uses Hermes' own platform sender, `tools.send_message_tool._send_to_platform`, the call cron delivery makes
(`cron/scheduler_delivery.py`), with a Telegram platform config holding the Talk bot token the server already has. It
formats, chunks and attaches files the way `hermes send` does. Talk stores no token.

Report states are pending, sending, sent, failed and uncertain, separate from the job state and the voice receipt.
The report and its destination are stored before the send. A send is claimed (pending to sending, stored) first; a
claim found after a restart becomes uncertain and is not resent. Telegram's sendMessage has no idempotency key, so a
timeout or any unclassified error is uncertain too, never retried. A send whose attachment upload reported a warning is uncertain too. Only errors that show nothing left (connection
refused, rate limit) go back to pending, at most `receipts.attempts` times, with the same stored text; the task never
reruns. A sent report records the Bot API message id.

Limits: `tasks.max_concurrent` (1..2) processes, `tasks.max_open_per_user` open jobs, `tasks.request_chars`,
`tasks.result_chars`, `tasks.max_turns` and `tasks.run_budget_seconds` (Hermes' own budget, then a stop 60 s later).
Every task route is rate limited per user and accepts 32 KiB.

### Decision trail

| Candidate | Grounding | Verdict |
| --- | --- | --- |
| Dashboard `/api/ws` JSON-RPC (`session.create`, `prompt.submit`), as Conduit iOS uses | Needs the dashboard running and a ticket or user token in the plugin; approvals arrive as server requests | Not taken: adds a credential and a second service to a standalone plugin |
| `tui_gateway` stdio JSON-RPC subprocess | Same methods; one long-lived child per server | Not taken: every task dies with the Talk process, so restart adoption is impossible |
| Embedded `AIAgent` in the Talk process | No public builder (private recipes in `hermes_cli/oneshot.py`, `cron/scheduler.py`); an in-process agent with no approval context auto-approves flagged commands | Not taken: unsafe by default and couples the voice server to agent work |
| `hermes chat` single-query process per task | Documented CLI (`--query-file`, `--format stream-json`, `--resume`, `--run-budget`, `--source`); single-query approvals fail closed; SIGINT ends with `error: Interrupted`; about 190 MB and 7 s per run here | **Taken** |

#### Current Hermes (gateway CLI): blocked

Hermes core `bf0203a7` routes `hermes chat` through a thin client for the canonical gateway (`hermes_cli/gateway_chat.py`).
That client refuses voice tasks before they start: `Unsupported gateway CLI options: --run-budget` (exit 2), and
points to `agent.run_budget_seconds` in the profile's `config.yaml`. The gateway's own `session.create` accepts
`run_budget`, but the client has no flag that forwards it. Three other differences from the single-query process
above also break the existing task contract:
- SIGINT to the client prints `Detached; accepted work continues at the gateway.` and does not send `session.interrupt`.
- A finite `-q` turn that reaches an approval gate detaches. It does not fail closed.
- Only `--oneshot` is unattended.

So Talk's stop/escalate cancel, its deadline backstop and its single-query approval guarantee no longer hold on that
path. A plugin-side repair would have to edit the profile config, drop the per-task budget, or replace `hermes chat`
with a gateway RPC client. This repo rules out all three, so the manager has to choose. Talk is unchanged and
still targets the single-query CLI (HomeLab core `e06df9c2`).

#### Current Hermes: gateway-RPC transport (manager-authorized 2026-11-03)

On a core that ships the public `hermes_cli.gateway_client`, Talk picks `hermes_talk/gateway_rpc.py` once at startup.
Otherwise it uses the CLI above. A gateway refusal fails the task and nothing ever falls back. A lost answer is not a
refusal: Talk resubmits the stored session, text and submission id, which returns the admission the gateway took.
The client attaches to the profile's own gateway, starting it when none runs:
- `session.create` with `run_budget`, or `session.resume` of the conversation's session.
- `prompt.submit` with the job id as `submission_id` and `finite=True`. The same id with other text is refused
  (`admission_conflict`), so a restart never starts a second run.
- Talk follows `prompt.receipt`. It stops with `prompt.cancel` or `session.interrupt`, retries a persisted cancel or
  deadline with a bounded backoff while the gateway is unreachable, and counts a job as stopped only once the
  receipt is terminal and the session is idle.

A finite turn is the core's native single-query context: flagged actions resolve through `approvals.single_query_mode`.
Talk never sets `unattended`. On these cores that flag is the `hermes -z` contract, which auto-approves flagged
actions; a real-gateway check caught it deleting a directory under `approvals.mode: manual`. `python3
verify-agent.py --rpc-target <pinned core source>` runs these checks against real gateway daemons with the scripted
local model.

The scripted model in `tests/agent/scripted_model.py` stands in for the provider only. Hermes' AIAgent, tools,
approvals and SessionDB run unchanged against it.

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
python3 verify-integrated.py --offline --require-host   # canonical gate: backend, frontend and Pulse offline, host mandatory
python3 verify-agent.py --require-hermes                 # voice tasks through the real Hermes agent (scripted model),
                                                         # Telegram reports (synthetic transport) and delivery faults
python3 verify-agent.py --browser <out-dir>              # Chromium delegation job: real app.js, server and tasks
python3 verify-integrated.py --browser <out-dir>        # legacy and Talk Chromium inventories, capped containers
python3 verify-frontend.py --mobile <out-dir>            # Chromium and actual WebKit: cold/warm assets, rotation, retry and native audio unlock
python3 verify-backend.py --offline     # full backend gate (below)
python3 verify-thread.py --offline --require-host  # chat input of the exact thread, reply voice, Shopy (real hook)
python3 verify-pulse.py --offline       # Pulse per-item acceptance through the real routes, memory and SessionDB
python3 verify-pulse-engine.py --offline  # Pulse engine parity: model selection, memory, follow-ups via ctx.llm (host runtime required)
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
   - activity scoping and status, and Pulse cards that only a named, answered mention acknowledges;
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
| Voice tasks: native agent, tools, delivery, restart | yes (off by default) | n/a | yes (real `hermes chat`, scripted model) | in fixture (named disposable profile) | no |
| Delegation wiring in the Talk UI (Chromium, local peers) | yes | n/a | yes (real tasks in the browser job) | n/a | no |
| Telegram task reports | yes (on with tasks) | n/a | yes (Hermes sender, synthetic Telegram transport) | default on, no egress | no |
| Exact-thread chat input (text, text files; truthful notes otherwise) | yes | yes | yes (registered gateway hook, adapter-built events, real SessionDB) | in fixture | no |
| `/talk` launch handle binding the exact chat/topic; visible unbound state | yes | yes | yes (real PTB Application dispatch, recording sender; real routes) | no (needs a gateway restart with this plugin) | no |
| Chat agent's reply about a photo or file carried into the same call | yes | yes | yes (real turn finalizer and `post_llm_call`, real SessionStore, native image payload; scripted reply) | no | no |
| Transcript and reply-voice selector in the Mini App | yes | yes (unit) | n/a | n/a | no |
| Shopy female/restrained source example | yes | yes | n/a | no | no |

## Outcome checklist

- [x] Authenticated, account-bound persistent conversation; reopen resumes; explicit new is isolated
- [x] Settled text and bounded summaries in native Hermes history; no audio, SDP or tool output
- [x] Scoped SOUL, allowlisted skills and truthful activity briefing within explicit budgets
- [x] Pulse: per-item exposure, mention and acknowledgement; selection, memory, feedback, follow-ups and
  deduplication per account (`verify-pulse.py`, memory and real SessionDB, plus the Chromium browser job)
- [x] Pulse engine: model selection, memory extraction and follow-up checks through the plugin's `ctx.llm`,
  degrading visibly without it (`verify-pulse-engine.py`, real runtime, deterministic local model)
- [ ] Pulse attribution and feedback with a real GPT-Live model, and the engine prompts on the profile's real
  model (manager)
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
(MIT); see `LICENSES/CONDUIT-LICENSE`. The delegation append framing and 500-byte chunking in `static/talk/app.js`
follow Conduit `GPTLiveProtocol.swift` at 4b6889097d53ad731089f255b03afe534ef3da45; the task flow (commentary
progress, speakable result on the same delegation id, call and task lifetimes kept apart) follows its
`GPTLiveDelegationBridge.swift` and `VoiceBackgroundJobs.swift`. No other Conduit code is copied. Pulse adapts the
mechanisms of Conduit notifier's Pulse at d25391f6382211dc22e90672c02ea2709112e3dc and the iOS `PulseFeed` at
b7fd1cc81bf72c6ef8c35a87cd6b15a135fabd5c (both MIT; `LICENSES/CONDUIT-NOTIFIER-LICENSE`,
`LICENSES/CONDUIT-LICENSE`). `pulse_memory.py`, `pulse_select.py` and `pulse_followup.py` port the notifier's
prompts and decision rules (adapted, under that notice); the rest is written for Talk.
