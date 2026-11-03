# 0003 Pulse: the whole applicable engine (learning, delivery policy, claims) and what stays out

Status: accepted for the experiment branch (2026-11-03, prime-talk-pulse-close-1104). Supersedes the
"unsupported_boundaries" list of the 2db7690 candidate, which left out ordinary backend mechanisms because
that phase was time-boxed.

## Decision

Port every upstream Pulse mechanism (Conduit notifier d25391f6, MIT, `LICENSES/CONDUIT-NOTIFIER-LICENSE`) whose
inputs exist on this surface, and leave out only those whose inputs or outputs are external to Talk. Each one
left out has a concrete reason below.

| Upstream | Talk | Status |
| --- | --- | --- |
| `pulse_prefs.py` (logistic preference model) | `hermes_talk/pulse_prefs.py`, byte-for-byte after the header | ported unchanged |
| `pulse_learning.py` ladder, DEFAULT_LEVELS, REASONS, lessons (level/too often, sooner/too late, seen/already knew, part/wrong time, rule/not relevant), signals from ratings | `pulse_policy.py` (`signal`, `reason`, `ceiling`, `pace`, `closed_parts`, `muted`, `seen_titles`); reasons parsed from the feedback delegation | ported; signals from voice ratings (thumbs, more = Do it sent, later none) |
| `pulse_candidates.route` levels (score, SURFACE_AT, PUSH_AT, URGENCY, quieter) | `pulse_policy.decide/arrange` over the model's picks at call setup | ported; push = lead the briefing, screen = after the leads |
| `pulse_quiet.py` (quiet days, quiet spell) | `pulse_policy.quiet_why`, config `pulse.quiet_days`/`quiet_until` | ported; held for the first brief after |
| `pulse_hours.py` night and parts of the day; hold unless overdue/soon | `pulse_policy.part/is_night`, config `pulse.night`/`zone` | ported; no night unless configured (a call is the user's own choice) |
| `pulse_brief.py` (held items delivered once, stamped) | `pulse_ledger._deliver` brief stamp per local day, `briefed` on cards | ported as the first eligible call of the day |
| `pulse_claims.py` + memory "claims" | `pulse_claims.py`, `pulse_memory` claims prompt, engine `_claims` | ported; judged against the session's own stored tool calls (SessionDB `get_messages`), unbacked claims become `[claim]` cards |
| `pulse_report.py` nudge bars | `pulse_select.BARS`, config `pulse.nudge` (balanced default) | kept from 0002, nudge now configurable |

## Left out, each for a concrete reason

- **Push/APNs, Lock Screen and push timing** (`pulse_push.py`, `pulse_delivery.py`, `pulse_hours.timing`/`push_cap`
  scheduling): Talk has no push surface and the packet forbids scheduled external messages. Delivery is the
  voice call the user opens; the ladder decides how a pick comes up in it.
- **Scheduled report and morning-brief turns** (`pulse_report.py` schedule, `pulse_brief.py` turn text with
  calendar/reminders sections): they post into a home chat on a timer, which would be a scheduled external
  message. The brief's holding and once-per-day delivery are ported into the call.
- **Proposals** (`pulse_proposals.py`): they write to the phone's calendar and reminders after an Add tap. Talk
  has no phone write surface, and adding one would be a new approval path and UI (AGENTS.md forbids both).
- **Look-ahead** (`pulse_ahead.py`): its inputs are the phone calendar, reminders, every-year dates and the
  Shortcuts inbox (iOS connectors, excluded). Talk's memory due dates already drive follow-up checks and bands.
- **Places, health, Shortcuts inbox, busy/driving** (`pulse_places.py`, `pulse_spots.py`, `pulse_hours.busy`):
  private iOS connectors, excluded by the packet.
- **Loudness from quick pushes** (`pulse_prefs.loudness`): ported, but Talk has no push, so no row carries
  `pushed` and it stays zero.
- **Learning resets and the Pulse dashboard** (`pulse_learning.reset`, `What Pulse learned` API): need a settings
  UI, which Talk does not have and must not add; `forget` is ported and unused.
- **Conduit chat claim release** (`conduit_claims.py`): about Conduit's dashboard owning a chat, unrelated to Pulse
  claims and absent here.

## Consequences

- One "not useful" moves a value-3 pick from the same place below SURFACE_AT (upstream math); a value-4 pick still
  comes up. The engine parity gate shows it, and that it holds across restart and never crosses accounts.
- Call setup writes Pulse state only when a card's delivery level changes or the day's brief is stamped, never on
  every call (the 2ff53d7 RPC timeouts came from a per-call write).
- Model steps remain the plugin's own `ctx.llm` with no tools, no override and no new billing (0002).
