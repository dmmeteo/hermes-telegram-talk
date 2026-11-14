# 0010 Three tasks of one call at once: the conversation's session plus parallel fresh sessions

Status: accepted for the source branch `default/talk-english-labels-1114` (2026-11-14). The default profile asks for
three running and five waiting voice tasks (`tasks.max_concurrent: 3`, `tasks.max_open_per_user: 8`).

## Context

`JobBoard.pump` skipped every queued job whose conversation already had a running one. The reason is the native
session: each task of a conversation resumes the conversation's stored Hermes session (`hermes chat --resume`, or
`session.resume` on gateway cores), so the agent keeps its context. One session takes one run at a time, so two
tasks of one call cannot share it. Raising `max_concurrent` alone would still run one task per call.

## Decision

- The oldest task of a conversation whose session is free takes it, as before. A task that starts while another
  task of its conversation holds the session is `parallel`: it runs in a fresh native session (no resume) with the same
  bounded voice turns in its prompt, and its session never replaces the conversation's (`Job.parallel`, persisted).
- Admission stays oldest first across the profile; a slot freed by any task goes to the oldest queued one.
- Parser bounds: `tasks.max_concurrent` 1..3 (profile-wide native runs), `tasks.max_open_per_user` 1..8 (queued and
  running per user). Defaults stay 1 and 3.

## Consequences

- A parallel task does not see what other tasks of the call did in their sessions, only the voice turns.
- Each running task is its own `hermes chat` process (about 200 MB each measured under the agent gate); three at once
  need that memory on the host.
- Restart, cancel, dedupe, report messages and results are per job and unchanged. A restarted Talk adopts live runs
  with their stored `parallel` flag. Jobs stored before this change load as not parallel.
- Tests: `tests/capacity/run.py` (real server, board, NativeAgent and spool with a bounded runner: 3 running + 5
  queued, ninth refused, promotion, cancel, restart) and `tests/e2e/run-agent.py` `one_call_three_native_runs` (real
  Hermes, three overlapping native sessions). The gateway transport's parallel `session.create` is not exercised here.
