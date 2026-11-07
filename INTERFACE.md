# Hermes Telegram Talk v2 shared interface
Base path /tg-voice/, same origin HTTPS. Every authenticated request carries initData validated server-side. No user-chosen profile/bot. JSON UTF-8; fixed application-owned routes only.
POST api/bootstrap {initData} -> {conversation_id, profile:{id,display_name,avatar_url}, appearance:{listen_color,speak_color}, greeting_enabled, limits:{event_batch_max}}. Must not return SOUL, skill bodies, raw history or credentials. UI may display avatar/name, but no transcript/status dashboard.
POST api/session {initData,sdp,conversation_id} -> {sdp,type:"answer",voice_session_id,conversation_id}. Backend composes SOUL/skills/resume/activity instructions and history before creating subscription call. No browser-supplied provider URL, instructions, model or billing. Keep existing WebRTC transport.
POST api/events {initData,conversation_id,voice_session_id,events:[{id,sequence,role,text}]} -> {accepted_ids:[...],conversation_id}. Only settled user/assistant text; idempotent server validation, bounded batch/text. No raw frames/audio/SDP/reasoning/tool output logging. Retain retryable pending batches across reconnect only within the verified same account/conversation, reject foreign ownership. Never replay task launches.
POST api/end {initData,conversation_id,voice_session_id,readiness?:{stages:{<stage>:ms},failure}} -> {ok:true}. `readiness` is optional and content-free (stage names, integer milliseconds and the unmet gate, e.g. `connect:handshake`); the server only logs it. Idempotent release/checkpoint; no task cancellation, browser pagehide best effort plus server-side expiry. Visibility hidden / screen lock is NOT End.
POST api/new {initData,conversation_id} -> same shape as bootstrap with new conversation_id. Not required as a primary UI button; available as explicit future command. Current frontend needs only avatar/ring + Mute and a one-tap launch/retry fallback if autoplay/mic requires user activation.
Errors use JSON {error:non_sensitive_actionable_message}, appropriate HTTP code, no raw upstream body. Workers may add backward-compatible public fields, not remove/rename these without manager decision. All tests use explicitly synthetic local fixtures; real Telegram initData is never generated in frontend source or auth bypassed in production.


## Backend notes (additive; the wire shapes above are frozen)

These notes are backend-owned and add no field. Changing or removing a field above needs a manager decision.

- **Implemented routes.** All five routes above exist in Talk mode (`server.py --config ...` or `hermes talk serve`). Without `--config` the server stays the original voice test and serves only `POST api/session {initData,sdp} -> {sdp}`.
- **Explicit compatibility.** In Talk mode, `api/session` also accepts a body without `conversation_id`. The backend then uses the account's current conversation, so the existing `static/app.js` keeps working until integration. The response still carries the full v2 shape. Any other extra field gets a 400.
- **Field rules.**
  - Bodies must contain exactly the listed keys.
  - `initData` must be signed, at most 1 h old, and from an allowlisted user before any state, credential or upstream access.
  - `Origin` must match exactly.
  - `events[].id` matches `[A-Za-z0-9_.:-]{1,64}`. `sequence` is an integer in 0..999999. `role` is `user` or `assistant`. `text` is 1..`event_text_chars` characters.
  - A batch holds 1..`limits.event_batch_max` events. The `events` route accepts up to 192 KiB; the others accept 32 KiB.
- **Idempotency.**
  - `accepted_ids` echoes every valid id in the batch, including ones already stored. A retried or out-of-order batch is safe.
  - A stored turn is never rewritten. Turns in one batch are stored in `sequence` order.
  - Events for an already-ended call of the same account and conversation are still accepted, which covers pending batches after reconnect.
- **Status codes.**
  - 400: malformed input or unexpected fields.
  - 401: bad or expired initData.
  - 403: Origin or allowlist.
  - 404: a conversation or voice session that this account doesn't own, or whose native session
    was deleted.
  - 409: a conversation that is no longer current, after `api/new`.
  - 413: body too large.
  - 415: not JSON.
  - 429: rate limit.
  - 502: upstream rejected the call or returned an invalid answer.
  - 503: no ChatGPT sign-in on the host. No API fallback exists.
- **Avatar.** `profile.avatar_url` is the same-origin path `/tg-voice/avatar`. It serves the configured local file (neutral SVG by default) with a sandboxing CSP.
- **Greeting.** The instructions tell the model to speak first only after the call connects. The frontend should request the first response once the data channel is open, and only if `greeting_enabled` is true.
- **Tasks.** Starting or cancelling Hermes work from voice is not supported in this build, and the instructions tell the model to say so. `End` (`api/end`) never cancels anything.
- **Fixtures.** `python3 -m hermes_talk.fixtures --port 8792` runs the real server with synthetic history, credential and upstream, and prints a signed synthetic initData. `hermes_talk.fixtures.sign_init_data(user_id, auth_date)` signs others. Real initData is never generated.

## Voice tasks (additive; present only when `tasks.enabled` is true in the Talk config)

- **Bootstrap/new** add `tasks: {available: bool}`. It is true only for a user in `tasks.user_ids` when the profile can run tasks now (see README). Without tasks in the config the shapes above are unchanged.
- Every route below takes signed `initData`, exact `Origin` and the allowlist first, then the account's own conversation and voice session. Guessed or foreign ids answer 404; a call that ended answers 409 where a live call is required; tasks unavailable answer 503 with the reason.
- `POST api/task {initData,conversation_id,voice_session_id,delegation_item_id,request}` -> `{job, created, channel, reply}`. Starts one native Hermes task for a delegation of the live call. The same delegation id in the same call returns the same job and starts nothing. `request` is the delegation text (1..`tasks.request_chars`). `"cancel task N"` cancels task N instead. The client appends `reply` on `channel` to the same `delegation_item_id`.
- `request` may end with a line `REPORT TO: <name>` naming a destination from `receipts.destinations` that lists this user (or `this chat`); the line is removed from the task. Any other name answers 403 and starts nothing. The browser never supplies a chat id; the request body has no destination field.
- `"result of task N"` returns task N's stored result as a speakable `reply` and starts nothing.
- `POST api/tasks {initData,conversation_id,voice_session_id}` -> `{jobs:[job], context_pending, tasks:{available}}`. `jobs` holds every open or not yet delivered job of the conversation, whatever its age, plus the latest ten.
- `job.report` (additive, null without reports) is `{state, destination}`: `state` is the chat report's own state (`pending`, `sending`, `sent`, `failed`, `uncertain`), separate from `job.state` (execution) and `job.receipt` (voice delivery); `destination` is `this Telegram chat` or the configured name.
- `POST api/task/cancel {initData,conversation_id,job_id}` -> `{job}`. Idempotent. Ending a call never cancels.
- `POST api/task/ack {initData,conversation_id,voice_session_id,job_id}` -> `{job}`. Sent after the client appended the result to the job's own delegation in the job's own call; repeats change nothing. Delivered means appended, not heard. A partial append (the data channel failed between chunks) is never acked or repeated in that call. A submission whose response was lost is resubmitted with the same `delegation_item_id`; only 400/401/403/404/409/413/415/429/503 on the first submission are treated as refusals, and a resubmitted delegation returns its existing job even if readiness lapsed.
- `POST api/context {initData,conversation_id,voice_session_id}` -> `{revision, text, sources:{jobs,activity}}`. Task outcomes and Pulse items the live call has not received; `revision` is null when there is nothing new. `POST api/context/ack {initData,conversation_id,voice_session_id,revision}` -> `{ok:true}` marks them read.
- `job` is `{id, number, delegation_item_id, voice_session_id, state, receipt, result, error, spoken}`; `state` is accepted, running, succeeded, failed, cancelled or interrupted; `receipt` is undelivered or delivered.

## Pulse (additive; the shapes above are unchanged)

- `events[]` may carry `interrupted: true` on an assistant event whose speech the user started talking over before
  it settled. Any other value, or the field on a user event, answers 400. Without the field nothing changes.
- Activity lines in instructions and context updates begin with a per-call label: `- P<n> [status] title (...)`.
  Labels continue across a call's context updates and restart at P1 in the next call. A follow-up from Pulse
  memory reads `- P<n> [followup] news (Pulse memory item m<id>, ...)`; a line the model selected may end its
  parenthesis with `why now: ...`. Both are additive text inside the instructions; no wire field changed.
- `POST api/pulse/feedback {initData,conversation_id,voice_session_id,delegation_item_id,request}` ->
  `{card:{handle,state,feedback}, channel:"speakable", reply}`. `request` is a client delegation of the form
  `pulse P<n> useful|not useful|dismiss|later|more`, optionally followed by a reason (`not relevant|already knew|too
  late|too often|wrong time`, additive). It needs the live call (409 after it ended) and a label that
  call carried (404 otherwise); a malformed request answers 400. The same delegation id in the same call returns its
  first outcome and changes nothing. The client appends `reply` on `speakable` to that delegation. It works whether
  or not tasks are enabled and never starts a task.
- `api/context/ack` marks the Pulse cards of that update exposed, never heard. A card is acknowledged only by a
  settled user turn after an uninterrupted assistant turn of the same call that named it, or by feedback.

## Exact-thread chat input and reply voice (additive; the shapes above are unchanged)

- A call continues only the Telegram conversation a `/talk` launch handle proves (issued by the profile's gateway
  in that private chat or topic). `api/session` and `api/voice` accept an optional `launch` string (the handle from
  the web_app button URL's `launch` query parameter); a signed `start_param` may carry the same handle. Without a
  valid handle the call is unbound and takes no chat input. The browser never sends a chat or topic id; any other
  extra field still answers 400.
- `api/voice` responses add `thread:{status}`: `bound`, `unbound` (no handle), `expired`, `used` (claimed by another
  launch), `invalid`, `foreign` (another profile, bot or user), `not_private` or `unavailable`. Reading it claims
  nothing.
- `api/context` responses add `sources.inputs` (chat inputs carried whole) only when an update carries chat input. Their text adds a `## Chat input` section
  with lines `- C<n> ...` for text and files the user sent in that exact thread during the call, oldest first, and
  truthful notes for what the voice cannot use (photos, unsupported, oversized or unreadable files). The same
  `api/context/ack` stores each carried input once in the conversation.
- `POST api/voice {initData, voice?}` -> `{voice:{current, default, choices:[{id,label}], applies:"next_connection"}}`.
  Without `voice` it reads; with one of `choices[].id` it stores the account's reply voice (400 otherwise). The
  voice applies when the next session is created; a live call keeps its voice until it reconnects.

