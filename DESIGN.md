# Hermes Telegram Talk

A dark English Telegram Mini App for a single authenticated voice conversation. The dialogue fills the screen above a compact persistent bottom row. Three dots open the call menu on the left, a larger round profile avatar sits in the center, and a round microphone control sits on the right. The app has no heading of its own. Telegram's outer container chrome stays outside the page.

The menu contains Reply voice and End call. Connect, Retry saving, Retry and Enable audio remain discoverable when needed. An unlinked launch shows "Not linked to a chat. Send /talk there to link it." A voice change during a call offers an explicit Reconnect. The catalogue qualification is visible in the menu.

The transcript shows interim text, final corrections, Interrupted and Cut off labels as before. It keeps at most 100 lines and scrolls through the remaining height. It follows new text while the reader is at the end and preserves older reading positions on updates and rotation. It is display only. Final turns retain their existing persistence path.

## Composition and decisions

The packet settles a calm dark mobile direction. The Stripe.dev quiet technical dossier informs neutral material, restrained light type and fine circular line art. Its desktop night-owl and mobile hero references were viewed. We omit the editorial grid and decorative console. This is a working mobile screen, not a copy of that landing page. No moodboard or variant exploration was needed.

Chosen tokens are background #101416, text #EDF2EE and secondary text #A7B6AE. Blue identifies measured microphone input and orange identifies measured assistant output for both profiles. Silence and unusable media states are neutral. The ring uses local and incoming Web Audio RMS measurements. Silence leaves a thin circle; energy changes its width and radius. There is no random or time-driven voice effect. A diamond marks connecting, a dot marks listening/speaking and a dash plus broken circle marks muted silence. Actual assistant output remains orange while the outgoing microphone is muted. The pressed microphone retains its light background and full-opacity dark icon during hover and press. Accessible labels identify each state. Reduced motion keeps fixed geometry while the measured audio still selects listening/speaking color and label.

The font is the device's system sans serif. No font download, third-party avatar or tracking request is introduced. An original inline SVG face is the fallback. The same-origin local fallback loads first. Authenticated same-origin `api/avatar` image bytes replace it through a short-lived blob URL. Touch targets exceed 44 px and keyboard focus has a visible outline. Portrait and landscape keep the same bottom row. The 52 px controls flank a 96 px avatar ring, reduced to 72 px in short landscape.

## Ownership and deployment boundary

`static/` remains the original voice test and its exact legacy verification. `static/talk/` is the v2 static bundle. The integrated server serves `static/talk/` at `/tg-voice/` whenever a Talk config is given and `static/` otherwise; `tests/browser/fixture.py` and the HTTP contract rely on that selection with no override. The frontend slice did not change the backend, stock Telegram, profiles or deployed assets.

The call resource object owns its peer, media tracks, data channel, AudioContext, analyser nodes, timer and animation callback. A replaced call cannot act on a new call. The authenticated conversation owns a bounded in-memory queue of settled text, sequence numbers and retry IDs. Reconnect revalidates bootstrap and retains events only for the same signed identity and conversation. Auth or ownership refusal discards queued events. No browser storage or audio recording is used. Failed saving exposes Retry saving after three bounded retries, with identical IDs and no new voice session.

## Protocol and readiness

Inspected primary source is Conduit `GPTLiveProtocol.swift`, `GPTLiveSession.swift` and `GPTLiveConversationController.swift` at revision `3a4a2ce461f7667e28ca75770ae10c8f166a4680` in https://github.com/kaishi00/hermes-conduit. Conduit-derived behavior remains under `LICENSES/CONDUIT-LICENSE`.

Subscription events use `session.started`/`session.updated`, `input_transcript.added`, `output_transcript.added`, and final `turn.done {turn:{role,transcript}}`. Only final user/assistant turns enter event ingestion. Fragment events, public API events, reasoning and tool output do not. Delegation gets an unavailable response through `delegation.context.append`, once per ID. It never launches or replays a task. Two nonvisual Pulse paths change no screen element. An assistant `turn.done` that follows user speech (`input_transcript.added`) during its output is sent with `interrupted: true`, so its text is not counted as telling an update. A `pulse P<n> ...` delegation goes to `api/pulse/feedback` once per ID, with its reply appended on `speakable` to that delegation.

Mic tracks are disabled until peer connection, open data channel, subscription handshake, running AudioContext and successful output playback. When greeting is enabled, a single supported `session.context.append` in the speakable channel requests the configured opening. Final assistant text plus quiet measured output completes the opening. This follows the inspected subscription protocol. No shared interface file was changed. The frozen bootstrap does not advertise the configured event text cap. This client uses the backend default of 2000 characters and preserves longer text in Unicode-safe chunks. Integration must retain that cap or expose the configured value before using a lower cap.

Autoplay denial or suspended audio keeps the mic disabled and exposes Enable audio. The browser may require that tap. A finite readiness deadline makes an absent handshake or greeting recoverable. Mute affects the actual outgoing track and survives retry, reconnect and temporary connection interruption within the page.

## Exit and screen lock

`visibilitychange` and Telegram minimized state do not end a call. A restored back-forward-cache document rebinds controls and initializes a fresh call. A real `pagehide` closes tracks, peer, channel, audio graph and animation, then checkpoints best effort using authenticated keepalive requests. Native Telegram closure and background termination do not reliably deliver unload events. Server expiry remains necessary. A physical iPhone, real screen lock and provider call have not been tested by this worker.

## Verification

Run `python3 verify.py --offline` for the unchanged 26-check legacy inventory. Run `python3 verify-frontend.py --offline` for that inventory plus deterministic frontend and real-backend HTTP checks against this checkout's backend. A backend that differs from `tests/frontend/backend-manifest.json` fails the gate. `TALK_BACKEND_SOURCE` can name another byte-identical backend checkout.

Browser verification runs both the original `python3 verify.py --browser OUT` and the Talk suite, each in a capped offline container; `python3 verify-integrated.py --browser OUT` runs both. `python3 verify-frontend.py --browser OUT` starts the Talk container itself, with `tests/browser/run.mjs` mounted at `/app/talk-job.mjs` beside the image's Playwright package. Both use real Chromium and local WebRTC peers. The visibility scenario launches an isolated native Chromium process and connects through loopback CDP with `noDefaults: true`. This avoids Playwright focus emulation that otherwise forces pages visible. It asserts the real hidden document state while media remains connected. No CDP port is published from the network-disabled container. Identities, context, signalling credentials, speech events and oscillator media are explicitly synthetic.

To change ring behavior edit `static/talk/app.js` draw/sample functions, then run the full offline and browser inventories. To change the screen edit `static/talk/index.html` and `app.css`, then inspect desktop, 390 px portrait, short landscape, focus and reduced motion screenshots.

The frontend slice's [task report](/home/me/.hermes/run/coding/prime-tgv2-front-1102/report.md) records its exact accepted source, screenshots and complete pass/fail inventory. Implemented and fixture-tested behavior is distinct from deployed and physical-device acceptance. Independent multi-model review is outside this worker's admitted single-model assignment; self-review and runtime verification are recorded without claiming that coverage.

## Compact UI review

The user-provided phone screenshot was inspected. Its oversized avatar, repeated heading and always-visible selector are deliberately replaced by the requested dialogue-first hierarchy. The existing dark palette, system font and restrained circular ring remain. See [ADR 0005](docs/adr/0005-compact-talk-media-and-avatar.md) for media, catalogue and avatar ownership.

The separate `tests/compact/run.py OUT` gate exercises both synthetic profiles, actual outbound WebRTC receiver energy, mute and reconnect, blocked output, long-dialogue geometry, reading position, menu, End call and unlinked launch. The canonical mobile gate continues to exercise Chromium and WebKit. Exact final revision screenshot paths and review dispositions are recorded in the delivery report under `/home/me/.hermes/profiles/prime/run/coding/prime-talk-compact-qa-1107/report.md`.
