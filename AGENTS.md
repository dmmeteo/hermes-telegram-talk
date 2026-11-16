# Agents

Hermes Telegram Talk PoC v2. Keep it small: `plugin.yaml`, `__init__.py`, `server.py`, `hermes_talk/`, `static/`,
`tests/` (all verification: `tests/e2e/run-*.py` entry points, `tests/legacy/` browser harness for the original
voice test), `docs/adr/` (indexed decisions), `LICENSE` (both Conduit MIT notices), `talk.example.yaml`.
`static/`, `tests/frontend/`, `tests/browser/` and `DESIGN.md` belong to the frontend owner.

Boundaries:
- No live provider calls, deployment, bot/webhook/profile changes or Hermes core edits from a worker.
- Never put credentials or private Pewds data (SOUL, memory, chats, profile) in source, browser, logs or evidence.
  Tests use synthetic homes, identities and text only.
- Signaling stays authenticated (signed initData + allowlist) before any state, credential or upstream access.
  Profile and bot come from config and the bot token, never from the request.
- Only GPT-Live subscription signaling at the fixed URL; never `/v1/realtime/sessions` or API billing.
- History goes through public `SessionDB` methods only: no SQL, no fake model turns, no raw audio.
- `INTERFACE.md` wire shapes are frozen; additive public fields only, renames need the manager.
- Keep the Conduit-derived code under its MIT notice (`LICENSE`).

Before committing: `python3 tests/e2e/run-integrated.py --offline --require-host` must print
`TALK_INTEGRATED_OFFLINE_OK`. It includes `tests/e2e/run-backend.py --offline` (`BACKEND OK`, with `tests/e2e/run-legacy.py --offline`
at `26/26`), `tests/e2e/run-frontend.py --offline`, `tests/e2e/run-pulse.py --offline` (`TALK_PULSE_OK`, memory and host),
`tests/e2e/run-pulse-engine.py --offline` (`TALK_PULSE_ENGINE_PARITY_OK`; TMPDIR must not be under `~/.hermes/profiles/`) and
`tests/logsecrets/run.py` (`TALK_LOG_SECRETS_OK`: no bot token in logs or stored errors), `tests/reports/run.py`
(`TALK_REPORTS_OK`, status messages) and `tests/titles/run.py` (`TALK_TITLES_OK`).
For chat-input, transcript or reply-voice changes, `python3 tests/e2e/run-thread.py --offline --require-host` must print
`TALK_THREAD_OK` (real gateway hook, adapter events and SessionDB; see `docs/adr/0004-exact-thread-chat-input.md`).
For opening, turn-taking or result-delivery changes in `static/talk/app.js` (its `Dialogue`), `python3 tests/e2e/run-dialogue.py`
must print `TALK_DIALOGUE_OK` (node on PATH; the real app.js against the real server, no provider).
Pulse model steps (selection, memory extraction, follow-up checks) call only the plugin's own host `ctx.llm` with no
override (see `docs/adr/0002-pulse-engine-model-route.md`); tests reach `tests/pulse/model_fixture.py` through it,
never a provider. Never add a model-role slot, credential or billing path for Pulse.
Pulse read state is per card and per call: being in the instructions is exposure, never hearing; only a named,
answered mention or feedback acknowledges a card (see README "Pulse" and `docs/adr/0001-pulse-per-item-evidence.md`). For task scheduling or capacity changes, `python3 tests/capacity/run.py` must print `TALK_CAPACITY_OK` (3 running
from one call in independent native sessions, the rest waiting uncapped; see `docs/adr/0010-parallel-tasks-of-one-call.md`).
For task kinds, T3 work, task controls or status-message visibility, `python3 tests/work/run.py` must print
`TALK_WORK_OK` (see `docs/adr/0011-t3-work-and-task-visibility.md`).
For task status titles or what the hook records of T3 admissions, `python3 tests/titles/run.py` must print
`TALK_TITLES_OK` and `python3 tests/reports/run.py` `TALK_REPORTS_OK` (see `docs/adr/0013-objective-task-titles.md`).
For task, context or delegation changes, `python3 tests/e2e/run-agent.py --require-hermes` must print `TALK_AGENT_OK` and
`python3 tests/e2e/run-agent.py --browser <out-dir>` `TALK_AGENT_BROWSER_OK`. Voice tasks run as native Hermes sessions:
either `hermes chat` single-query processes or (manager-authorized 2026-11-03, for current cores whose CLI rejects
`--run-budget`) a thin plugin-side client of the canonical public gateway RPC (`session.create` with `run_budget`,
`session.interrupt`, finite single-query turns under the profile's own `approvals.single_query_mode`; never
`unattended`, which on current cores is the `hermes -z` auto-approve contract). Never bypass the gateway, copy core internals, add an
approval path or UI, use `--yolo`, change profile policy or run an in-process agent. For UI or lifecycle changes, also run
`python3 tests/e2e/run-integrated.py --browser <out-dir>` (legacy and Talk Chromium, capped containers) and never weaken
its assertions.
