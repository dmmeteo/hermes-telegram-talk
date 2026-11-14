# 0011 Voice tasks: confirmed T3 work, uncapped waiting requests and two status-message switches

Status: accepted for the source branch `prime/talk-work-model-1114` (2026-11-14). Supersedes the open cap of 0010
(`tasks.max_open_per_user`, 3 running + 5 queued); the parallel lanes of 0010 stay.

## Context

A voice task runs one native Hermes turn. The live profiles give tasks the `hermes-telegram` toolset, which includes
`t3_control`, and their policy says "start, then wait". So a coding request's turn waited on T3 inside the one
execution slot: quick requests queued behind it, the open cap refused a fourth request, and at the run budget Talk
stopped the turn and reported the task failed while T3 kept working. Telegram posted every request, quick or not.

## Decision

- **Large means observed T3 work.** The Talk plugin's `post_tool_call` hook sees each `t3_control` call of a task's own
  turn: in the `hermes chat` child (which carries `HERMES_TALK_JOB`) or in the gateway (session marker written before
  submission; a turn whose context was compressed continues in a child session, found through its
  `parent_session_id` chain). Only `ok` start/send results, or a lost answer T3 may have committed, are recorded, one
  file per request_id under `plugin-data/hermes-talk/work/<job>/`. Reads, waits, refusals and calls outside a task record
  nothing. No phrasing, keyword, duration or model claim decides it. `t3_control` stays the only launcher.
- **Talk follows the work.** A large job's turn holds no execution slot. When the turn ends the job stays running;
  `turn` keeps what the turn alone would have settled to. One follower thread reads each linked thread through the
  profile's own `t3_control` handler (registry dispatch, no tool hooks) and settles the job from T3: completed (result
  is the thread's newest message when it is the assistant's), failed, interrupted, or cancelled when a cancel was asked
  and T3 shows the run stopped.
  Blocked stays open (❓). A link is its own T3 run, identified by run id (`active_id`, else the newest `latest_id`
  that is not the run it was admitted behind), with the run count as fallback when T3 shows no ids. T3's read is per
  thread, not per run, so a run's outcome is read only while it is the thread's latest run. When a later run started
  before Talk saw it end, when its run cannot be singled out, when T3 shows only `idle`, or when T3 no longer has the
  thread, the outcome is **unknown**: the job is stored as `interrupted` with `unknown` in its sidecar, no result, an
  explicit reason, spoken as "how it ended is not known" and posted as ❔. It is never reported completed and never
  carries another run's text. A cancel interrupts only while the active run is the link's own. A possibly admitted start is confirmed only by the thread existing; a possibly
  sent message only by T3 showing its run. A lost start that named no thread is kept as a note on the result.
  Nothing is ever resent.
- **Execution stays bounded, waiting is not capped.** `tasks.max_concurrent` (1..3) bounds turns of jobs that are not
  large. At most as many turns of large jobs stay live beside them; past that the oldest such turn is stopped (its T3
  work goes on), so live turns never exceed twice the setting. `tasks.max_open_per_user` is removed. The HTTP rate
  limit stays the abuse guard.
- **Controls are not tasks.** The voice model delegates English control lines (`CONTROL: list | status N | result N |
  cancel N`) whatever language is spoken. They are answered from server state before readiness and never queue. The
  legacy "cancel task N" and "result of task N" still work. The task prompt lists earlier tasks' T3 threads so an
  update can be sent to the same thread; a send to a thread another task owns is recorded as continuing it.
- **Two switches per account.** `receipts.large_work` (default on) and `receipts.micro_tasks` (default off) set the
  defaults; `/talk_notify` in the user's private chat with the bot shows both as buttons, for task users only, and
  stores the choice per (profile, bot, user) in `plugin-data/hermes-talk/notify.json`. A switch decides whether a task
  without a message gets one. An existing message follows its task to the end. A still-open task gets its message at
  its next status change after its switch turns on. A finished task is never posted. A hidden quick task that becomes
  T3 work is posted once, as running. A hidden task keeps a `hidden` report record, which no desk version sends.
- **Rollback-safe storage.** The job record keeps exactly the fields of the deployed revision `35f8fa1`, whose loader
  drops any record with an unknown key. `parallel`, `links` and `turn` live under `hermes_talk:jobwork:<id>`.

## Consequences

- The task prompt asks the agent to end its turn after the admission; a turn that keeps waiting lives until its run
  budget or until it is the oldest beyond the live-turn bound.
- When a later queued message of the same thread starts before Talk's next read (10 s), the earlier task ends as
  unknown, since no supported read shows how that run ended. Updates queued on a busy thread therefore often leave the
  earlier task unknown; T3 itself keeps the full history.
- A start whose answer was lost before T3 named a thread cannot be followed. Killing a turn between T3's commit and
  the hook's write (cancel, budget) loses that admission the same way. Both settle from the turn.
- If the voice server has no `t3_control` (plugin missing), large work stays running until it can read T3.
- Rollback: the old Talk loads every job. A job that was following T3 (turn ended, still running) has no admission or
  live process in its base record, so the old Talk marks it interrupted and never reruns it; its T3 thread keeps
  running and is visible in T3. Hidden tasks keep their inert `hidden` record, so the old desk posts none of them;
  tasks created after the rollback post as before. The old parser needs no YAML change (`max_open_per_user` absent
  means 3). The new receipts keys must be removed before a rollback, since the old parser refuses them.
- Tests: `tests/work/run.py` (real server, board, desk, NativeAgent, spool and hook body; bounded runner and a
  file-backed T3), `tests/capacity/run.py` (lanes, no open cap), and `tests/e2e/run-agent.py` `t3_admission_real_hook`
  (a real turn calls a synthetic `t3_control` plugin; the real hook fires; Talk follows through the registry).
