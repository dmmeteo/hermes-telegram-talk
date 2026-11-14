# 0008 The call's dialogue follows Conduit: speak-first opening, one idle-gated speakable queue, exactly-once delivery

Status: accepted for the source branch `prime/prime-talk-dialogue-1114` (2026-11-14). Live release unchanged; Prime owns
deployment and live verification.

## Context

`static/talk/app.js` sent every speakable (task replies, job results, speakable context, the greeting request) the moment
it had one. A result could cut into the user's sentence or the greeting, two results went back to back, and the client
asked for the opening at connect although the server instructions (0007) already open the call. That is a duplicate
greeting.

Conduit (`kaishi00/hermes-conduit` at 4b6889097d53ad731089f255b03afe534ef3da45) runs one controller per call:
`GPTLiveConversationController.swift` `sendOpeningIfNeeded`/`checkOpening` (no request when the host applied the
greeting, one fallback after 4 s of silence, a 10 s hold), `isConversationIdle` and `flushPendingContextIfIdle` (one FIFO,
commentary sent at once, one speakable per model turn), and `GPTLiveDelegationBridge.swift` (results on their own
delegation; a replaced call drops unsent items, which return as notices on the next call).

## Decision

- One lazy `Dialogue` per call in `app.js` owns who is speaking, the opening state (idle, held, asked, owed, done) and a
  keyed `Map` outbox. Every data-channel write except the tasks-unavailable refusal goes through it.
- `greeting_enabled` means the server applied a speak-first opening. Nothing is sent at connect; one request after 4 s of
  audible silence; if audio was blocked through the 10 s hold, one request when it returns and only if the model is
  still silent.
- Commentary goes at once. A speakable waits until the model is quiet (or 6 s past its last output fragment or an
  unanswered prompt), 1 s after the model's turn ended, 2 s after user speech, and up to 6 s for an open user turn.
- Settle callbacks run only after the real append: a job is acked after a whole append, a partial one is never repeated
  in that call. `stop()` drops unsent items unacked; the server re-offers them through `api/context`.
- `refresh()` holds one speakable context update at a time, until it is appended and acked.
- Not taken from Conduit: resending an unsaid result (ack stays "appended"), the 8 s End drain (End stays immediate),
  the late-result prefix.

## Verification

`python3 tests/e2e/run-dialogue.py` (`TALK_DIALOGUE_OK`): `tests/dialogue/check.mjs`, the lifecycle, delivery and
frontend harnesses, and `tests/dialogue/e2e.py`, the real `app.js` against the real server with a Ukrainian speak-first
opening (no request when the model self-greets, one when it stays silent, none with the greeting off).
