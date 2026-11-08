# 0006 Same-call task queue: isolated native sessions, claimed writer scopes, ordered result ingestion

Status: experiment on the local branch `feature/concurrency-1108` (2026-11-08). Off by default. Not deployed.

## Context

One voice call should be able to keep three Hermes tasks working at once and ten more waiting. Before this change
every task of a conversation resumed the conversation's one native session (`_task_prompt` returned it as `resume`)
and settlement rebound that session to whatever the task ran in. Two tasks of one call would have been two writers
of one native history, so the scheduler ran one task per conversation (the same-conversation guard) and the config
allowed at most 2 running and 5 open tasks.

## Decision

`tasks.execution` chooses how a task runs, and the job freezes the choice when it is accepted.

- `canonical` (default, unchanged): the task continues the conversation's session. Bounds stay 2 running, 5 open.
- `isolated`: the task gets its own native session (`session.create` on the gateway, a fresh `hermes chat` run on
  the CLI transport). Its context is built and frozen when the call delegates it: the bounded voice turns and the
  conversation's earlier task outcomes, never another task's live session. The job stores the context and its
  SHA-256 until it is terminal, so a queued or restarted job runs on exactly what the call said when it delegated.
  It never resumes or rebinds the conversation's session. Bounds: 3 running, 13 open per user.

The same-conversation guard became a claims relation (`jobs.claims`, `jobs.overlap`). A canonical job claims its
conversation's session, as before, and the whole workdir against isolated jobs only (a canonical job left over from
before a switch never runs beside an isolated writer). An isolated job claims its writer scope in the task workdir:
none for a request with a `READ ONLY` line, the path for `WRITES: <path>` (resolved through symlinks), the whole
workdir otherwise. A job waits while a running job or an older waiting job holds an overlapping claim, so conflicting
jobs start in delegation order; a job that conflicts with nothing ahead of it may pass. A path that resolves outside
the workdir, or more than one directive, is refused before anything is stored. Launch checks the frozen context
against its stored SHA-256. A claim is a promise between sibling tasks, not a sandbox: the toolsets are unchanged, so a task
that says `READ ONLY` and then writes is not stopped. Universal write isolation is not claimed.

Results merge only through the persisted job and its receipts. When a job becomes terminal it takes the next
`result_seq` of its conversation under the board lock; instructions and context updates hand outcomes over in that
order, each once, and a delegation result is acknowledged only in the call that delegated it. A repeated completion
or delivery changes nothing, because terminal transitions are refused and receipts are idempotent. No task writes a
user or assistant turn into the conversation's history.

Origin identities (profile, bot, user, conversation, call, generation, delegation, job) stay on the job beside the
execution identities (native session, admission). `api/tasks` jobs gain `generation` and `result_seq` (additive).

## Alternatives

| Candidate | Verdict |
| --- | --- |
| Per-job isolated session, frozen context, ordered receipts | **Taken** |
| Fork the canonical session per job and merge back | Not taken: needs a core fork API that was not verified, and the merge-back is the racing writer again |
| Readers isolated, writers in the canonical session | Not taken: two lifecycles for one queue, and writers still serialize per conversation |

## Verification

`python3 verify-agent.py --rpc-target <SDK> --concurrency --receipts <dir>` prints `TALK_CONCURRENCY_OK`. It runs the
Talk server against the pinned core's real gateway with the labelled scripted model (no provider): thirteen tasks from
one call (three running in three native sessions at one instant, ten waiting, the fourteenth refused with 429),
queued and running cancels, a duplicate delegation, a reconnect with a stale call, out-of-order completion,
ordered and deduplicated context ingestion, writer-scope conflicts, a Talk kill and restart with three admitted and two
queued jobs (adopted, never replayed, frozen context unchanged), a lost admission acknowledgement and cross-user and
cross-profile denial. A delivery receipt means the result was handed to the voice model, not that it was heard.
