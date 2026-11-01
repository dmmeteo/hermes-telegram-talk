# Pewds Telegram voice test (PoC)

A throwaway Telegram Mini App for one question: is real hands-free voice conversation inside
Telegram on an iPhone useful? This is a **voice test associated with Pewds, not a Pewds agent bridge**.
The voice model gets a short public test persona (Ukrainian by default). It has no Hermes tools,
memory, SOUL, chat history or profile data. When the model tries to delegate, the page answers
honestly that nothing was executed.

One dark mobile page with three buttons: Start, Mute and End. There's no transcript, status panel,
waveform, history or settings. The page stays idle until Start. Start asks for the microphone and plays
the incoming WebRTC audio. Mute toggles the mic track. End, or leaving the page, stops every track
and closes the peer and data channel.

## Shape

- `server.py`: Python 3.10+ standard library only. Serves `static/` under `/tg-voice/` and one
  signaling endpoint, `POST /tg-voice/api/session` `{initData, sdp}` → `{sdp}`.
- Before touching credentials or the upstream, the endpoint requires all of the following:
  - an exact `Origin` match and a JSON body ≤ 32 KiB with exactly `initData` and `sdp`;
  - a Telegram `initData` HMAC that is valid and fresh (≤ 1 h old);
  - a user id in `TG_VOICE_ALLOWED_USER_IDS`.

  After that come the limits: a per-user mint limit (6/min), at most 2 concurrent exchanges and a
  10 s body read timeout. `initDataUnsafe` and query ids are never trusted.
- Upstream: only ChatGPT-subscription GPT-Live,
  `https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas`,
  with `Authorization`, `ChatGPT-Account-Id` and `OpenAI-Alpha: quicksilver=v2`. It sends `{sdp, session}`
  and gets back an SDP answer plus a `Location` call id. There's no redirect following, no API billing and no
  fallback. Credentials come from `hermes_cli.auth_codex.resolve_codex_runtime_credentials` at request
  time, server side only. Logs carry method, path, status and exception type, never SDP, bodies,
  initData, tokens or data-channel events.
- The exchange code is adapted from Hermes Conduit `plugin_api.py` (MIT, see `LICENSES/CONDUIT-LICENSE`).

## Run (manager, on the VPS)

```sh
# Environment comes from the existing Pewds credential source; never put tokens on argv or in files.
export HERMES_HOME=/home/me/.hermes/profiles/pewds
export TG_VOICE_BOT_TOKEN=...            # Pewds bot token
export TG_VOICE_ALLOWED_USER_IDS=...     # numeric Telegram ids, comma separated
<hermes-python> server.py --host 127.0.0.1 --port 8791 \
    --origin https://her.dmmeteo.dev --base-path /tg-voice/
```

Use the Hermes Python that has `hermes_cli` importable. Put the reverse proxy in front with
`https://her.dmmeteo.dev/tg-voice/` → `127.0.0.1:8791/tg-voice/`, keeping the path. Pass `--host`
only for a private Docker bridge address. There's no polling, no webhook change and no gateway restart.
The Mini App button, BotFather and the proxy belong to the manager.

## Verify

```sh
python3 verify.py --offline                 # 26 behavioral security/lifecycle checks, synthetic creds, no network
python3 verify.py --browser <out-dir>       # real Chromium + real server + fake provider, --network none container
```

The browser run uses `browser/fixture.py`: the real server with a synthetic credential resolver and a
fake upstream that relays the offer to `browser/provider.html`, a second Chromium page that answers
with a 440 Hz oscillator. That makes it offline evidence for UI and lifecycle, **not live voice proof**.
Headless Chromium needs `--use-fake-ui-for-media-stream` to capture at all, and that flag ignores
permission denial. So the real `NotAllowedError` path runs in a separate full-Chromium browser started
with `--deny-permission-prompts`. The same-page path from denial to retry to connected uses one injected
`NotAllowedError` in the fixture.

## Outcome checklist

- [x] Start / Mute / End only, dark, mobile, idle until Start
- [x] Signed initData, freshness, allowlist, Origin, body, field, rate and concurrency checks (offline)
- [x] Mute, End, restart, permission denial, network failure and page exit in real Chromium (offline fixture)
- [ ] Real GPT-Live call from the VPS with the Pewds sign-in (manager/human)
- [ ] Hands-free use inside Telegram on an iPhone (human verdict: useful or not)
