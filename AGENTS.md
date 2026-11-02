# Agents

Hermes Talk PoC v2. Keep it small: `plugin.yaml`, `__init__.py`, `server.py`, `hermes_talk/`,
`verify-backend.py`, `verify.py`, `browser/` and `static/`. `static/`, `tests/frontend/`, `tests/browser/`
and `DESIGN.md` belong to the frontend owner.

Boundaries:
- No live provider calls, deployment, bot/webhook/profile changes or Hermes core edits from a worker.
- Never put credentials or private Pewds data (SOUL, memory, chats, profile) in source, browser, logs or evidence.
  Tests use synthetic homes, identities and text only.
- Signaling stays authenticated (signed initData + allowlist) before any state, credential or upstream access.
  Profile and bot come from config and the bot token, never from the request.
- Only GPT-Live subscription signaling at the fixed URL; never `/v1/realtime/sessions` or API billing.
- History goes through public `SessionDB` methods only: no SQL, no fake model turns, no raw audio.
- `INTERFACE.md` wire shapes are frozen; additive public fields only, renames need the manager.
- Keep the Conduit-derived code under its MIT notice (`LICENSES/CONDUIT-LICENSE`).

Before committing: `python3 verify-backend.py --offline` must print `BACKEND OK`. It includes
`verify.py --offline` (`26/26`). For UI or lifecycle changes, also run `python3 verify.py --browser <out-dir>`
in the capped container and never weaken its assertions.
