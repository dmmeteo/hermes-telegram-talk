# Agents

Throwaway PoC. Keep it small: `server.py`, `static/`, `verify.py`, `browser/`.

Boundaries:
- No live provider calls, deployment, bot/webhook/profile changes or Hermes core edits from a worker.
- Never put credentials or private Pewds data (SOUL, memory, chats, profile) in source, browser, logs or evidence.
- Signaling stays authenticated (signed initData + allowlist) before any credential or upstream access.
- Only GPT-Live subscription signaling at the fixed URL; never `/v1/realtime/sessions` or API billing.
- Keep the Conduit-derived code under its MIT notice (`LICENSES/CONDUIT-LICENSE`).

Before committing: `python3 verify.py --offline` must print `26/26`. For UI or lifecycle changes, also
run `python3 verify.py --browser <out-dir>` in the capped container and never weaken its assertions.
