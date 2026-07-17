# Fork the agent_1 prompt for text rather than parametrize a shared prompt

- Status: accepted
- Date: 2026-07-17
- Deciders: owner, Claude Code

## Context and Problem Statement

The text-thesis adapter needs an A1 claim-classification prompt. The existing `prompts/agent_1.md` is written
for video: it frames the input as "the transcript of one episode of a US equity markets show", describes an
`## On-Screen Text` block sourced from keyframes, handles Whisper transcription imperfections, and carries a
narration-driven `published_at` override. None of that applies to a hand-written thesis. But the *rest* of the
prompt — the `SignalSetDraft` schema, the five claim categories, the three-tier classification rules, the
tie-breaking rule, the ticker-normalization rules, and the self-check — is entirely source-neutral and must
stay identical across sources so both adapters emit the same `SignalSetDraft` contract.

Question: fork `agent_1.md` into a separate `agent_1_text.md`, or parametrize a single shared prompt by
source-framing?

## Decision Drivers

- The video-specific material (on-screen text, transcript provenance, Whisper handling) is interwoven through
  the framing sections, not cleanly isolatable.
- The shared behavioral sections must not diverge between sources, or two sources would tier the same claim
  differently while both producing schema-valid output — a divergence no test would catch.
- The repo has **no prompt-templating infrastructure** today; prompts are flat `.md` files loaded verbatim by
  `prompt_loader.system_prompt`.

## Considered Options

- **Fork** — copy `agent_1.md` to `agent_1_text.md`, rewrite the framing sections, keep the behavioral
  sections identical.
- **Parametrize one prompt** — a single prompt file with conditional/templated source-framing, rendered per
  adapter.

## Decision Outcome

Chosen: **fork**, because the video-specific framing is interwoven (not a clean parameter) and no templating
infrastructure exists to make parametrization anything but a larger, speculative build (rejected under the
no-versions/final-form ethos — parametrization would be designing infrastructure for a second source that
already fits the fork). `agent_1_text.md` keeps sections 4 (schema), 5 (categories), 6 (tiers), 7 (why
tiering), 8 (behavioral rules), and 12 (self-check) behaviorally identical to `agent_1.md`.

### Consequences

- Good: each prompt reads cleanly for its own source; no conditional logic a model has to reason around.
- Good: adding a third source (PDF, newsletter) is another independent fork, consistent with the additive
  source-adapter pattern (ADR 0002).
- Bad: the shared behavioral sections now live in two files and can silently diverge. **Mitigation:** a
  short sync-obligation note at the top of *both* prompts points at this ADR and lists the shared sections, so
  a change to the schema or tiering rules is made in both. If the number of source prompts grows past two or
  three, revisit with an extracted-shared-fragment approach (which would then be worth the templating build).

### Confirmation

Both prompts load (`prompt_loader.system_prompt("agent_1")` / `("agent_1_text")`, pinned non-empty by
`tests/unit_tests/test_prompt_loader.py`). The sync-obligation note is the standing guardrail against drift;
this ADR is the durable record of *why* the duplication exists, so the note itself stays a pointer rather than
a rationale.
