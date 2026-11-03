# 0001 Pulse for Talk: per-item evidence and a voice adaptation of Conduit Pulse

Status: accepted for the experiment branch `experiment/pulse-1103` (2026-11-03); its Selection, Memory and Follow-ups
rows and the "no separate selection inference" alternative are superseded by
[0002](0002-pulse-engine-model-route.md). Live release unchanged.

## Context

The opening briefing was one cursor per conversation. The first stored assistant turn of a call, typically the
greeting, moved the cursor past every item the instructions carried, so items that were never mentioned, clipped
by the model, interrupted or abandoned were lost. Reproduced on base 249ff1a: three items, a greeting and a user
"hi", and the next call offered none of them.

Upstream reference, inspected at pinned heads (both equal to the handoff heads, MIT):

- Hermes Conduit notifier d25391f6382211dc22e90672c02ea2709112e3dc (`pulse*.py`, `dashboard/api/pulse.py`).
- Hermes Conduit iOS b7fd1cc81bf72c6ef8c35a87cd6b15a135fabd5c (`PulseFeed.swift`, `AppState+Pulse.swift`,
  `V2PulsePage.swift`, `PulseNotifications.swift`).

## Decision

Talk gets a Pulse engine with one record per account and separate evidence for every step:
`candidate` → `exposed` (the voice model received the card whole) → `mentioned` (an uninterrupted settled assistant
turn of that call named it) → `acknowledged` (a settled user turn followed, or feedback). Closed states are
`dismissed`, `superseded` and `duplicate`; `snoozed` returns when its follow-up is due. Nothing infers hearing
from captions or audio. Task results carried in instructions follow the same rule: they are delivered only when
an uninterrupted assistant turn names "task N".

Decisions live in `hermes_talk/pulse.py`, the per-account record in `hermes_talk/pulse_ledger.py` (SessionDB meta),
the wiring in `hermes_talk/talk.py`, and the client marks interrupted assistant turns and routes `pulse ...`
delegations in `static/talk/app.js`.

## Upstream capability map

| Mechanism | Upstream (server / iOS) | Talk | Status |
| --- | --- | --- | --- |
| Selection | LLM selection call over remembered and found items, value 1..5, `MAX_ITEMS=5`; score `learned + 0.5·(value−3) + 2.5·picked`, surface at 1.25 (`pulse_candidates.py`, `pulse_prefs.py`) | Deterministic importance: status base, follow-up and unconfirmed boosts, unheard-exposure fatigue, learned preference; `activity_items` cap; oldest-first ties. The voice model is the speaker and gets a "Pulse updates" prompt section | implemented, adapted: no separate selection inference (GPT-Live subscription only, no API billing) |
| Memory | Extracted items (commitment, promise, intention, interest, fact, watch) with evidence and decay; signals and lessons (`pulse_memory.py`, `pulse_store.py`) | Per-account learned preference (source counts, bounded topic weights) and a told list for dedup; persists across reopen, restart, `api/new` | implemented, adapted: no model-based extraction of commitments from chats (needs separate inference) |
| Candidates and importance | Sources chat, calendar, reminders, notes, jobs, tools; logistic preference model | Profile sessions in configured sources, scoped to the user; fingerprinted per revision; importance above | implemented; calendar, reminders, notes, health and location are off (private connectors, iOS-only) |
| Cards | `cards` table, kinds followup/noticed/report/brief/proposal, clipped fields, 500 kept | Card per session revision with status, title, source, time, evidence log; 500 kept; per-call labels `P<n>` | implemented |
| Follow-ups | Checks for promise/commitment/watch with backoff, give-up and resolve (`pulse_followup.py`) | `later` snoozes 3 h (upstream "later today") and returns labelled; acknowledged `active` cards are watched and their outcome is labelled a follow-up | implemented, adapted: no model-driven checks |
| Feedback | Thumbs up/down with reasons, done, acted, not now; CAS idempotency (`dashboard/api/pulse.py`, `pulse_learning.py`) | `pulse P<n> useful / not useful / dismiss / later / more` via a client delegation; idempotent per delegation id; teaches source and topic weights | implemented; reasons `too_late`/`wrong_time` need schedules that voice does not have |
| Deduplication | Event-once keys, open-card skip, worded matching within 30 days, reminders once (`pulse_candidates.py`) | Fingerprint per (session, status, normalized title); supersede on change; same news from another session within 10 min of an unread or told card is a duplicate | implemented |
| Delivery accounting | Tagged attempts; delivered = a reply row after the tagged turn; iOS seen/acted (`pulse_delivery.py`, `PulseFeed.swift`) | exposure per call and channel, mention per event, acknowledgement = user turn after the mention in the same call; interrupted turns recorded, not evidence | implemented |

Not ported, with reason: push/APNs levels and Lock Screen privacy (Telegram Mini App, no push), places and spots
(location), health, calendar proposals and Shortcuts inbox (private connectors, off by assignment), claims and the
tool ledger (need Hermes tool hooks or core changes), scheduled reports, morning brief, quiet hours and nudge
levels (Talk briefs only when the user opens a call), and the iOS Pulse page (the accepted Talk UI has no
dashboard).

## Alternatives

- Keep the cursor and require "any assistant turn after the greeting": still marks unmentioned items read.
- Ask the model to emit an explicit marker per item: spoken output cannot carry hidden markers and GPT-Live
  `turn.done` has only role and transcript.
- A separate selection model call per call open: would need inference outside the GPT-Live subscription.

## Consequences

- An item is repeated until a call shows it was told and answered, or the user dismisses or snoozes it. A model
  that paraphrases a title beyond recognition leaves the item unread; that errs toward repeating, not losing.
- Attribution is lexical (whole title, or most title words including one that tells the card apart).
- Evidence: `verify-pulse.py --offline` (memory and real SessionDB), the updated backend and agent inventories,
  and the Chromium agent job step `pulse-interruption-and-feedback-cross-the-real-client`.
- Unresolved: behavior with a real GPT-Live model (paraphrase rate, interruption timing) is not exercised.
