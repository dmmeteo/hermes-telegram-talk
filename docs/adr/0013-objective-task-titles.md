# 0013 Task status titles name the admitted objective, retry until the host route answers, and never show the speech

Status: accepted (2026-11-16). Supersedes the fallback and "never changes" parts of 0009; keeps its route.

## Context

Live default-profile posts for large T3 tasks showed the first words of the spoken Ukrainian request, for example
"Так... Нє, ну загалом оця от типу апка в принципі працює…". The 0009 mechanism was deployed, but it had never
named a single task: the host log shows no `talk_task_title` call. A large task's report is created when T3
admits its work. That often happens right after a Talk restart, before the Pulse worker binds the host `ctx.llm`
route (about 30 to 60 s). The one naming attempt then failed, and the request's words became the title for good,
with no log line. A restart also lost the in-memory request, so a later attempt named the 60-character prefix.
The model was only ever given the speech. The work the task's turn actually admitted (the `t3_control` start's
title and message, or a send's message) was not recorded, and an English request was shown verbatim, filler included.

## Decision

- The data boundary: the post_tool_call hook (`work.observe`) records each admission's intent (a start's title and
  message head, a send's message head; never a reused thread's earlier title) on its link. The report desk owns
  the title (`receipts.ReportDesk`). The Telegram text is only `mark + title`.
- Final at once, with no model call: a usable `TITLE:` line, or a request that is itself a short plain English
  sentence (at most 10 words, at most 80 characters).
- Otherwise the desk stores `name_input` (`receipts.name_source`: the admitted work, newest last, then the redacted
  request) with the report, and asks the host model through the same route as before (`ctx.llm`, purpose
  `talk_task_title`, no override, 20 s). The model is asked for one imperative English sentence stating the
  objective of the newest admitted work, with filler left out, no translation of the opening words, and no
  claimed outcome. The answer is redacted and kept only if it is a usable title, whole and never cut.
- The first attempt runs before the first send (statuses passed meanwhile are replayed, as in 0009). If it gives
  nothing, the message is sent with a provisional title: the newest admitted start's own title when usable, else
  "Hermes task N". It is never the speech. The desk retries up to `TITLE_ATTEMPTS` (5) with backoff (10, 20, 40,
  60 s). The schedule is stored, so a restart resumes it from the same input. A later usable answer renames the
  one message with one edit. Once named, the title is never asked for again.
- After the last failed attempt, the provisional title stays, a note is stored on the report, and one warning is
  logged with the reason and no content.

## Consequences

- No new provider, credential, endpoint, model role or billing path. At most 5 host calls per large untitled task,
  none for trivial speech (short English, or a usable `TITLE:`).
- The scheduler and admission never wait on the model: naming runs in the report's own drive thread outside every
  lock. Task identity, one editable message and the lifecycle statuses are unchanged.
- A short English request is still its own title even when T3's work says more, for example "do it".
- Semantic quality depends on the profile's live model and is not verified offline. The gates use a synthetic host
  model (`tests/titles/run.py`) and the real host `PluginLlm` against the scripted local model
  (`tests/e2e/run-agent.py`, `t3_objective_title_real_hook`).
