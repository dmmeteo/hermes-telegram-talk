# 0015 The voice persona is the profile's SOUL identity, bounded

Status: accepted (2026-11-16). Supersedes the persona-source part of ADR 0007.

## Context

ADR 0007 stopped reading SOUL and gave every profile without `persona.text` a nameless default ("a calm and friendly
voice assistant"). The frame named only "the Hermes profile \"Michael\"". The default profile, whose SOUL opens with
its identity, answered as a generic assistant, and the user asked (voice task 43) for Talk to speak as the SOUL
identity, not as ChatGPT. A whole SOUL does not belong in a voice call: the default SOUL is about 14 KB, mostly
operational rules (placement, memory, delivery) that a voice cannot act on and that would crowd the 12000-character
instruction budget.

## Decision

- Persona source order: `persona.text` (an explicit per-profile override), else the SOUL identity, else the short
  default. The call log's `context frame` line reports `persona_source` (`config`, `soul` or `default`), never text.
- The SOUL is read per call by the core loader `agent.prompt_builder.load_soul_md(home_override=<profile home>)`, which
  scans it and pins the read to the serving profile. It is redacted like skills.
- Only the identity reaches the call: the lead before the first `## ` heading, clipped to 1200 characters with
  `[clipped]`, plus the `## Voice` section when it fits whole. Other sections never do.
- Both frames carry a name rule from `profile.display_name`: if asked who it is, the voice is that name and never
  introduces itself as ChatGPT or a generic assistant. It sits in the frame, so the persona cannot override it.
- The opening line stays configuration: `greeting.phrases` (each at most 160 characters), else the language line.
  A configured phrase is said exactly as written, in the language it is written in and untranslated (voice task 46:
  the default profile opens with the English "Hey, Vsauce, Michael here" and then speaks Ukrainian). Its opening
  policy no longer names the call language, and the language rule then reads "after the configured opening line,
  speak <language>" instead of "open the call in <language>".

## Alternatives

- The whole SOUL, clipped to a budget: rejected. The first 4000 characters of the default SOUL are mostly reasoning
  and teaching rules; the clip boundary is arbitrary and operational text would follow.
- A model-written condensation: rejected, as in ADR 0007; call setup never waits for a model.
- A static greeting that names the profile, without the persona: rejected. The call would still answer as a generic
  assistant after the first sentence.
