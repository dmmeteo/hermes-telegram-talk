# 0007 Call initialization follows Conduit: initial_items, a background summary, a voice persona and a configured opening

Status: accepted for the source branch `prime/prime-talk-init-1113` (2026-11-13). Live release unchanged; Prime owns
integration, deployment and live verification.

## Context

Talk composed every call from one instruction string: the first `soul_chars` (4000) characters of the profile's
engineering SOUL, a "summary" made of the latest user lines (`store._summary`, the last 40 rows, user role only),
and the last `recent_turns` turns pasted as `user: ...` / `assistant: ...` text. The opening was the English
`greeting.text` plus English rules, with the language hint in a separate 200-character section. A Ukrainian
profile without `greeting.phrases` therefore opened in English.

Conduit (iOS app `kaishi00/hermes-conduit` at 4b6889097d53ad731089f255b03afe534ef3da45, host plugin
`hermes-conduit-notifier` at 23ce63d16465cf06e15b19902ce403c29fb0e3e7) does this differently. Sources read:

- `dashboard/api/gpt_live.py:343-354` `gpt_live_session_config`: `{model, instructions, audio: {output: {voice}},
  delegation: {type: "client"}}` plus `initial_items` when there is history; `:68`, `:522` keep the last 40 items.
- `Conduit/Voice/GPTLive/GPTLiveProtocol.swift:269-277` `historyItem`: `{type: "message", role, content: [{type:
  "output_text" for assistant else "input_text", text}]}`.
- `Conduit/Voice/VoiceHistory.swift:742-838` `VoiceResumePlan`: up to 24 turns and 6000 characters word for word;
  otherwise the newest turns (at most 10, within 6000 characters) word for word and a summary of the older ones;
  a stored `{text, covers}` summary serves while at most 10 turns behind; turns clipped to 1200 characters; the
  summary prompt; a 24000-character summary input. `:840-883` the summary-only instruction block in
  `<previous_conversation>` for an engine that seeds the turns as history.
- `Conduit/Services/AppState.swift:9859-9907` `voiceResumeContext`: one-shot summary (`task: "compression"`,
  `maxTokens: 600`), stored on the host; never made from a partial read; on failure the call resumes from the
  latest turns alone.
- `dashboard/api/gpt_live.py:540-570`: the opening policy appended last ("Opening policy: speak first ... Say:
  \"...\" ... This applies whatever the persona above says"; `GPT_LIVE_WAIT_FOR_USER` without a greeting).
- `Conduit/Voice/GPTLive/GPTLiveConversationController.swift:136,170`: the `<hermes_persona>` framing and its escape.

## Decision

- `hermes_talk/resume.py` ports `VoiceResumePlan` with its constants and prompt. `prepare` reads the
  conversation's stored rows (public `SessionDB.get_messages` through `history.rows`, user and assistant text only,
  redacted) and sends the planned turns as `initial_items`. No dialogue goes into the instructions.
- The summary is written by the plugin's own host model (`ctx.llm` through `pulse_model.PulseModel`, purpose
  `talk_resume_summary`, no override, ADR 0002) and stored as `resume_summary: {text, covers}` in the conversation's
  meta state. Because `complete_structured` answers JSON, the prompt adds one sentence asking for
  `{"summary": ...}`, and the token cap is 1200 instead of Conduit's 600 so the wrapping cannot cut the text.
- A resume reads at most 10000 stored rows. A longer conversation is a partial read, as past Conduit's page cap:
  the stored summary keeps speaking for the start and is never replaced by a summary of the window. Turns between
  that summary and the newest ones then reach no call. This needs a row count from the history surface to lift.
- Deviation from Conduit, for Talk's startup invariant ("opening a call never waits for a scan or a model",
  `tests/startup/backend.py`): the summary is written in the background, when a call ends and when a call opens and
  finds one needed, deduplicated per conversation. Until it exists the call gets its latest turns and, if a stored
  summary ends before them, that summary of the start. Conduit instead waits for the summary at setup.
- Superseded by [ADR 0012](0012-voice-persona-from-soul-identity.md) for the default persona source.
- Deviation from Conduit, by the assignment: Talk no longer reads SOUL. The persona is `persona.text` (YAML, at most
  1200 characters) or a short default in the tone of Hermes' `LIVE_PERSONA`, inside Conduit's `<hermes_persona>`
  framing. `TalkRuntime(soul=...)` is still accepted from frontend-owned harnesses and ignored.
- The Conduit preface "and wait for the user to speak first" is left out of the summary block, because Talk's own
  opening policy, placed last, decides who speaks first.
- `voice.language` (`en`, `uk`) names the language in a leading language rule and in the opening policy. Each
  language has its own opening line, used when `greeting.phrases` is empty. `greeting.text` applies only without
  phrases and without a language. With the greeting off, the policy is Conduit's wait-for-user text.
- `context.budgets.soul_chars` is retired. Existing YAML that sets it still loads.

## Session parameters and delegation types

Verified wire shape means the key is written by source code we read. It does not mean the subscription endpoint
accepts it live; no provider call was made for this change.

| Key | Talk sends | Evidence |
| --- | --- | --- |
| `model` | yes, `gpt-live-1-codex` by default | Conduit host `gpt_live.py:57,346`. The Codex client omits it on `session.update` (`session_json(model: None)`). |
| `instructions` | yes | Conduit host `:347`; Codex frameless `session_json` `"instructions"`. |
| `audio.output.voice` | yes, the account's chosen voice | Conduit host `:348`; Codex frameless `session_json` `audio.output.voice` typed `RealtimeVoice`. Talk offers the nine subscription voices in `config.SUBSCRIPTION_VOICES`. |
| `delegation.type: "client"` | yes | Conduit host `:349`; Codex frameless `session_json` writes only `"client"`. The model's `delegation.created` items with `target: "client"` come back to the browser, which answers with `delegation.context.append` (`GPTLiveProtocol.swift:93-113,141-161`). |
| `delegation.ack_filler` | no | Codex frameless `session_json`, optional boolean. Not in Conduit. |
| `initial_items` | yes, when the conversation has turns | Conduit host `:351-353`; Codex frameless `session_json` maps roles `user`/`developer` to `input_text` and `assistant` to `output_text`. Talk sends only `user` and `assistant`, as Conduit does. |
| language, locale, transcription language, turn detection, tools | no | Not in either Conduit tree or the Codex frameless builder. Language stays instruction text; no key is guessed. |

The public OpenAI SDK `SessionConfigParam` (`types/live/session_config_param.py`) also lists `input` (not
`initial_items`), `client.data_channel`, `store`, `triggers` and delegation types `client`, `responses` and
`decisions`. That is the API-key endpoint (`/v1/live/sessions`), not the subscription endpoint Talk is limited to,
so it is not evidence for any of those on the subscription session. Only `delegation.type: "client"` is
substantiated for the subscription path.

The Codex sources were read from Prime's downloaded copies (`codex-frameless-config.rs`, from
`codex-rs/codex-api/src/endpoint/realtime_websocket/methods_frameless_bidi.rs` on `openai/codex` main, and the
ts-rs `RealtimeVoice` catalogue), not from a pinned revision.

## Alternatives

- Summary at call setup, as Conduit does: rejected for the startup invariant above. The cost of the background
  design is that one call after a crash or a long reconnect may lack the newest summary.
- Keep the extractive summary as a fallback: rejected. It listed user requests only, without answers, decisions or
  open work, and the model route already degrades to the latest turns.
- A model-written condensation of SOUL: rejected. The assignment allows the host model only for the summary.
