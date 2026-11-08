# 0002 Pulse engine: model selection, memory extraction and follow-up checks through the host's ctx.llm

Status: accepted for the experiment branch `experiment/pulse-1103` (2026-11-03). Supersedes the Selection,
Memory and Follow-ups rows of 0001 and its "no separate selection inference" alternative. Live release unchanged.

## Context

0001 delivered per-item evidence but replaced upstream Pulse's model mechanisms with deterministic stand-ins:
status-weight ranking labelled as selection, topic counters plus a told list as memory, and a three-hour snooze as
follow-ups. The user scope names the selection model and prompts, memory, follow-ups and the rest of the engine;
Prime rejected the reduced engine. The "no API billing" premise behind 0001 was an accidental restriction: the
user forbids new billing, not inference through an already-authorized runtime.

Interfaces inspected on the target core (`prime-tgt-cli-1103/target-sdk`): `hermes_cli.plugins.PluginContext.llm`
returns `agent.plugin_llm.PluginLlm`, the host-owned facade over `agent.auxiliary_client.call_llm` on the
profile's active model and auth. Provider, model, agent and auth-profile overrides are fail-closed behind
`plugins.entries.<id>.llm.allow_*_override`; a plugin that passes none runs on the profile's own model. This is the
call upstream Pulse makes (`pulse.py` `_memory_llm` → `ctx.llm.complete_structured`). `hermes_cli.plugins.
discover_plugins()` is the public loader that runs each enabled plugin's `register(ctx)`.

## Decision

All Pulse model calls go through hermes-telegram-talk's own `ctx.llm`, with no override and no task slot
(`hermes_talk/pulse_model.py`). `register(ctx)` binds it; the voice server, which `hermes talk serve` starts under
the same Hermes interpreter and home, calls `discover_plugins()` once from the Pulse worker, 30 s after it
started serving (never before: discovery of every enabled plugin takes seconds, and a server that accepts but
cannot answer broke the restart-adoption RPC check), so the binding exists in its process. Until then call setup
uses the labelled fallback. No account, credential, billing meter, auxiliary slot or model role is added. When nothing is bound or a
call fails, each step degrades and records it (`selection.by = "fallback"` with the reason, memory
`by = "unavailable"`, follow-up `failures` with retry): deterministic ranking is never reported as the model's.

The engine ports upstream modules at d25391f6, adapted at their boundaries:

| Upstream | Talk | Ported | Adapted or left out |
| --- | --- | --- | --- |
| `pulse_memory.py` `INSTRUCTIONS`, `KINDS`, `SPEAKERS`, `_item`, `_closed`, `_row_with`, `_due`, same_as/replaces | `pulse_memory.py` | prompt (six kinds, evidence quote, line, explicit, due/due_words, same_as, replaces, closed), speaker rule, "evidence must be on a line", "never a guessed date", repeat detection, handoff rows ("Pulse's reminder" lines are never items) | UTC instead of a user zone; `every_year`/`occasion`, `place`, `claims` fields dropped (no phone, places or tool ledger here); decay by kind half-life instead of upstream's expiry table |
| `pulse_candidates.py` `INSTRUCTIONS`, `parse`, `route`, lessons; `pulse_report.py` `BARS` | `pulse_select.py` | "remembered" entries (bring_up, value 1-5, skip done/covered/raised/rated/minor, line, why), nudge bars, "what Pulse brought up lately", lessons from feedback, refs limited to what was shown, value-ordered picks capped by the call's item budget, over-limit/skipped/not-picked/unanswered per card | "found" entries from calendar, reminders, notes, jobs and failed tools are not read; the report is the opening of a voice call |
| `pulse_followup.py` `INSTRUCTIONS`/`COMMITMENT_INSTRUCTIONS`/`WATCH_INSTRUCTIONS`, `DECISIONS`, `first_check`, `next_wait`, `BACKOFF_S`, `GIVE_UP_S`, `MAX_CHECKS`, `TRIES`, `RETRY_S`, `check` | `pulse_followup.py` | the three prompts, tell/done/drop/wait, first-check timing (commitment +1 day or due day 09:00, promise/watch +30 min), backoff with the model's hint, failures retried without counting, give-up | "tell" becomes a follow-up card that goes through selection and per-call evidence instead of a push; `when`/`urgent`, places, inbox and act/held checks left out |
| `pulse.py` tick, `_maybe_memory`, `_memory_run` | `pulse_engine.py` | read quiet chats, then due follow-ups, then select; one run at a time; model calls outside the store lock | a worker thread per voice server, woken when a call ends and every 5 min; call setup makes a bounded (8 s) selection when the cached one no longer matches the eligible cards |

Cards, per-call evidence, feedback and deduplication stay as in 0001 (`pulse.py`, `pulse_ledger.py`). New links:
a follow-up card acknowledged in a call marks its memory item reminded; `dismiss` on it cancels the item;
`not useful`/`useful` titles become selection lessons; Pulse's own spoken follow-up is never re-extracted.

Still not ported, with reason: push/APNs and Lock Screen levels (no push on a Mini App), places and spots
(location), health, calendar, reminders, notes and Shortcuts inbox (private connectors, excluded), claims and the
tool ledger (they watch tool calls in the agent process through `pre/post_tool_call` hooks, which exist as public
plugin hooks, but Talk's tasks run as separate native sessions; left out of this time-boxed correction, not
claimed), scheduled reports, morning brief, quiet hours and nudge levels as schedules (Talk speaks only when the
user opens a call; the nudge bar is kept as the selection's "balanced" bar), and the iOS Pulse page.

## Alternatives

- `hermes chat` single-query or gateway RPC sessions for each model call: would write Pulse prompts as user turns
  into SessionDB and run an agent loop with tools for a JSON classification. Rejected.
- Registering an auxiliary task slot (`register_auxiliary_task`): adds a model-role picker entry, which the packet
  forbids. Not used; a later deployment can add one deliberately.
- A selection call per call open with no cache: adds the full model latency to every call setup. The cache is
  keyed by the eligible cards' fingerprints, so a call reuses the background selection when nothing changed.

## Consequences

- Loading plugins in the voice server (from its Pulse worker) runs every enabled plugin's `register(ctx)` there too, as the `hermes` CLI
  already did before it exec'd the server. A plugin with side effects at register time would see a second
  process.
- Model calls cost the profile's normal model usage: one selection when the eligible cards change, one memory
  read per quiet session with new lines, one check per due follow-up (at most 6 per run).
- Evidence: `verify-pulse-engine.py --offline` (gate `pulse-engine-parity`, `TALK_PULSE_ENGINE_PARITY_OK`) runs
  the product routes on SessionDB under a real Hermes runtime with the plugin installed and enabled; the model is
  `tests/pulse/model_fixture.py`, a deterministic local OpenAI-compatible server reached only through
  `ctx.llm` → `call_llm`. Real-model behavior (prompt quality, paraphrase, cost) is for Prime's live
  qualification.
