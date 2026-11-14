# 0009 Telegram task status titles: an English sentence from the host model for other languages

Status: accepted for the source branch `default/talk-english-labels-1114` (2026-11-14). Supersedes "Nothing is
translated" and the "Hermes task N" fallback in README "Task reports in Telegram".

## Context

Every voice task keeps one Telegram status message: a mark and a title, then edits at each lifecycle step. The title is the
voice model's optional `TITLE:` line, else the request when it is English, else "Hermes task N". Ukrainian requests
often come without a usable `TITLE:`, so the status channel, which the owner uses to watch which tasks are queued,
running, done, failed or cancelled, showed only numbers.

## Decision

- An untitled request in another language is stored with its own words and `naming = true` on the report record.
- The job's one drive worker, before the only send, asks the plugin's host `ctx.llm` (`TalkRuntime.model`, the route of
  0002, no override) once with the redacted request: `receipts.TITLE_INSTRUCTIONS`, purpose `talk_task_title`, 20 s.
- The answer is redacted and kept only if it is plain English of at most eight words (`receipts.usable_title`). Otherwise
  (no route bound yet, outage, other language, too long) the request's own words stay the title.
- The title is stored before the send and never changes afterwards, so queued, running, completed, failed and cancelled
  all show the same text. A restart before the send asks again; nothing is resent.

## Consequences

- No new provider, credential, billing path or model role: the profile's own model, as Pulse and the resume summary use.
- An untitled non-English task's first status message waits for that one call (bounded by 20 s). It still shows the
  first status (queued); the statuses the job passed meanwhile are kept in order and applied as edits right after it,
  so no lifecycle step disappears because of the naming.
- The voice model still sees "Hermes task N" lines in its own context; numbers stay the handle for "cancel task N".
- Live semantic quality depends on the profile's model and is not verified offline: tests use a synthetic host model
  (`tests/reports/run.py`) and the real host `PluginLlm` against the scripted local model (`tests/e2e/run-agent.py`).
