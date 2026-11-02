# Hermes Talk

A small English Telegram Mini App for a single authenticated voice conversation. The screen has a profile face, an audio ring, the profile name and Mute. Enable audio or Retry appears only when needed. There is no transcript, history, settings, account picker or task control.

## Composition and decisions

The packet settles a calm dark mobile direction. The Stripe.dev quiet technical dossier informs neutral material, restrained light type and fine circular line art. Its desktop night-owl and mobile hero references were viewed. We omit the editorial grid and decorative console. This is a working mobile screen, not a copy of that landing page. No moodboard or variant exploration was needed.

Chosen tokens are background #101416, text #EDF2EE and secondary text #A7B6AE. Listening and speaking colors come from authenticated bootstrap. The ring uses local and incoming Web Audio RMS measurements. Silence leaves a thin circle; energy changes its width and radius. There is no random or time-driven voice effect. A diamond marks connecting, a dot marks listening/speaking and a dash plus broken circle marks mute. Accessible labels identify each state. Reduced motion keeps fixed geometry while the measured audio still selects listening/speaking color and label.

The font is the device's system sans serif. No font download, third-party avatar or tracking request is introduced. An original inline SVG face is the fallback. Only the same-origin `/tg-voice/avatar` path can replace it. Touch targets exceed 44 px and keyboard focus has a visible outline. Portrait centers the face above Mute. Short landscape places the face and action side by side.

## Ownership and deployment boundary

`static/` remains the original voice test and its exact legacy verification. `static/talk/` is the v2 static bundle. `tests/browser/fixture.py` imports the accepted backend and explicitly sets its test-only `STATIC_DIR` to that bundle. Integration must serve the contents of `static/talk/` at `/tg-voice/` alongside the accepted Talk backend. This worker does not change the backend, stock Telegram, profiles or deployed assets.

The call resource object owns its peer, media tracks, data channel, AudioContext, analyser nodes, timer and animation callback. A replaced call cannot act on a new call. The authenticated conversation owns a bounded in-memory queue of settled text, sequence numbers and retry IDs. Reconnect revalidates bootstrap and retains events only for the same signed identity and conversation. Auth or ownership refusal discards queued events. No browser storage or audio recording is used. Failed saving exposes Retry saving after three bounded retries, with identical IDs and no new voice session.

## Protocol and readiness

Inspected primary source is Conduit `GPTLiveProtocol.swift`, `GPTLiveSession.swift` and `GPTLiveConversationController.swift` at revision `3a4a2ce461f7667e28ca75770ae10c8f166a4680` in https://github.com/kaishi00/hermes-conduit. Conduit-derived behavior remains under `LICENSES/CONDUIT-LICENSE`.

Subscription events use `session.started`/`session.updated`, `input_transcript.added`, `output_transcript.added`, and final `turn.done {turn:{role,transcript}}`. Only final user/assistant turns enter event ingestion. Fragment events, public API events, reasoning and tool output do not. Delegation gets an unavailable response through `delegation.context.append`, once per ID. It never launches or replays a task.

Mic tracks are disabled until peer connection, open data channel, subscription handshake, running AudioContext and successful output playback. When greeting is enabled, a single supported `session.context.append` in the speakable channel requests the configured opening. Final assistant text plus quiet measured output completes the opening. This follows the inspected subscription protocol. The backend INTERFACE note suggesting `response.create` describes a public-API event that the subscription decoder does not use. No shared interface file was changed. The frozen bootstrap does not advertise the configured event text cap. This client uses the backend default of 2000 characters and preserves longer text in Unicode-safe chunks. Integration must retain that cap or expose the configured value before using a lower cap.

Autoplay denial or suspended audio keeps the mic disabled and exposes Enable audio. The browser may require that tap. A finite readiness deadline makes an absent handshake or greeting recoverable. Mute affects the actual outgoing track and survives a temporary connection interruption.

## Exit and screen lock

`visibilitychange` and Telegram minimized state do not end a call. A restored back-forward-cache document rebinds controls and initializes a fresh call. A real `pagehide` closes tracks, peer, channel, audio graph and animation, then checkpoints best effort using authenticated keepalive requests. Native Telegram closure and background termination do not reliably deliver unload events. Server expiry remains necessary. A physical iPhone, real screen lock and provider call have not been tested by this worker.

## Verification

Run `python3 verify.py --offline` for the unchanged 26-check legacy inventory. Run `python3 verify-frontend.py --offline` for that inventory plus deterministic frontend and real-backend HTTP checks. Missing or altered accepted-backend source fails the gate. `TALK_BACKEND_SOURCE` can name the byte-identical frozen snapshot after transfer.

Browser verification runs both the original `python3 verify.py --browser OUT` and the Talk suite in an admitted offline container. The Talk container command is `TALK_CAPPED_BROWSER=1 python3 verify-frontend.py --browser OUT`, with `tests/browser/run.mjs` mounted at `/app/talk-job.mjs` beside the cached Playwright package. Both use real Chromium and local WebRTC peers. Identities, context, signalling credentials, speech events and oscillator media are explicitly synthetic.

To change ring behavior edit `static/talk/app.js` draw/sample functions, then run the full offline and browser inventories. To change the screen edit `static/talk/index.html` and `app.css`, then inspect desktop, 390 px portrait, short landscape, focus and reduced motion screenshots.

The [task report](/home/me/.hermes/run/coding/prime-tgv2-front-1102/report.md) records the exact final source, screenshots and complete pass/fail inventory. Implemented and fixture-tested behavior is distinct from deployed and physical-device acceptance. Independent multi-model review is outside this worker's admitted single-model assignment; self-review and runtime verification are recorded without claiming that coverage.
