# Compact Talk controls, voice catalogue and bot avatar

The phone dialogue fills the available height above a persistent control row. The app removes its own heading. Telegram owns the outer chrome. The row contains a menu, a larger profile avatar and a microphone button. Reply voice and End call are in the menu. Connect, Retry and Enable audio remain visible when needed. An unbound menu launch still displays the instruction to send `/talk` in the intended chat.

Mute disables the outgoing microphone tracks. Its page-local preference survives call replacement, voice reconnect and audio suspension. It uses no browser storage. Assistant playback stays enabled. A ready call with measured microphone energy is blue, measured assistant output is orange, and silence is neutral. Actual assistant playback takes priority over outgoing microphone mute. Muted silence and unusable media states are neutral. Both profiles use these semantic ring colors, even when an older profile appearance config supplies another accent.

The transcript keeps its 100-line bound and final-turn persistence. Its scroll area fills the remaining height. New text follows the end only while the reader follows the end. Rotation preserves a reader's older position.

## Catalogue evidence

The native [Conduit VoiceTypes.swift](https://github.com/kaishi00/hermes-conduit/blob/685e1b6597e59f0f5b280f41e743191c3090df4d/Conduit/Voice/VoiceTypes.swift) declares nine `GPTLiveVoice.all` entries. Its [GPTLiveViews.swift](https://github.com/kaishi00/hermes-conduit/blob/685e1b6597e59f0f5b280f41e743191c3090df4d/Conduit/Views/GPTLiveViews.swift) selects those entries for subscription calls and a different `apiNames` list for API-key calls. The nine subscription choices are Arbor, Breeze, Cove, Ember, Juniper, Maple, Sol, Spruce and Vale.

Talk offers exactly that native subscription list, with configured labels. The profile default stays first. Pewds defaults to Cove. Shopy defaults to Sol with feminine self-reference. `api/voice` still persists only per profile, bot and user. A live choice requires explicit Reconnect in the same conversation. No billing or credential route changes.

This is a native-client catalogue, not an authoritative enumeration for `gpt-live-1-codex` at the fixed subscription URL. No public model-specific enumeration was established without authenticated probing. [OpenAI's ChatGPT Voice help](https://help.openai.com/en/articles/20001274-chatgpt-voice) also lists regional Live voices and qualifies availability by account and region. Those display names do not establish IDs or acceptance for this subscription endpoint, so Talk does not invent IDs for them.

## API-key voices are refused (2026-11-08)

Shopy shipped with the API-key voice `marin` as a configured addition. Every Shopy call then reached the SDP answer, remote track and playback, but never `peer`, `channel` or `handshake`. A real-application Chromium probe showed ICE staying in `checking` with no inbound RTP and no data-channel message. Pewds, on the same ChatGPT account and client bytes, used `cove` and reached `ready`. Conduit's `VoiceTypes.swift` keeps `apiNames` (marin, cedar, and others) for API-key calls only and states that GPT-Live refuses a voice the account lacks when the call starts. The fixed subscription route therefore admits only the nine subscription voices. Config load rejects any other `voice.voice` or `voice.choices` entry, so the failure appears at deployment instead of as a silent call that never connects.

## Bot image ownership

`hermes_talk/avatar.py` owns one cache per configured `VoiceApp`. The cache reads the configured bot's own ID and token, never request-supplied credentials or URLs. Authenticated `POST api/avatar` returns same-origin image bytes. The existing GET avatar continues to serve the configured local fallback. The frontend loads the local fallback immediately and refreshes the authenticated bot image independently of microphone and signaling startup.

The background worker uses Telegram's documented [getUserProfilePhotos](https://core.telegram.org/bots/api#getuserprofilephotos) and [getFile](https://core.telegram.org/bots/api#getfile) operations. Only `api.telegram.org` is contacted. Redirects and arbitrary file paths are refused. JSON responses are capped at 32 KiB, images at 1 MiB and each network operation at four seconds. Remote images must be PNG or JPEG. One refresh can run per cache. Successful results, including no photo, expire after five minutes. Errors preserve the last good result and retry after thirty seconds. The page polls every thirty seconds, with an early second fetch after startup. No token-bearing file URL or upstream exception reaches the browser or logs.

The capability is fixture-tested. Actual Telegram bot-photo visibility and physical-phone audio/background behavior remain unqualified. A missing or unavailable photo uses the local fallback. It does not block voice startup.
