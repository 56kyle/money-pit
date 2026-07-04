# A4 analysis: Step-1 disposition threading and macro-read-as-narrative

- Status: accepted
- Date: 2026-07-02
- Deciders: owner, architecture author

## Context and Problem Statement

Wave S3 restores A4's output to the `docs/architecture.md` §6.5 **container** (surviving
theses + dropped-claim records + the macro read + an optional halt), reversing the flat
per-step list. That container-vs-list flip is a settled straight revert (logged in
`docs/reviews/phase-1-6-findings.md`) and needs no ADR. Two decisions taken _while_ restoring
it, however, exceed a mechanical revert and encode determinism-preserving choices with real
rejected alternatives — recorded here (the 0003/0004 bar):

1. Sizing currently hardcodes `verified=False` (`pipeline/analysis.py`), haircutting **every**
   position as unverified. Threading A4's Step-1 disposition changes capital output.
2. §6.5 says the container carries "the macro indicators it read," but §8a/§9/§11.4 have the
   deterministic post-processor assemble `MacroIndicators` from `initial_answers` and forbid
   LLM-reported numbers from feeding regime/sizing.

## Decision Drivers

- Determinism and the evidence-only rule (§11.4): model-reported numbers must never feed
  regime classification or sizing.
- Principle 4 (`docs/pipeline_contracts.md`): JSON authoritative, markdown for humans.
- Pair every capital-behavior change with a failure-mode/behavior test.

## Decision Outcome

**1. Add `Step1Disposition(str, Enum) = {SUPPORTED, UNVERIFIED, CONTRADICTED}`.** A surviving
`ThesisJudgment` carries `disposition ∈ {SUPPORTED, UNVERIFIED}`; a `CONTRADICTED` claim is
dropped at Step 1 and recorded as a `DroppedClaim`, never a thesis. Sizing uses
`verified = disposition == SUPPORTED`, replacing the hardcoded `False`. **Consequence: supported
theses size larger** (the `haircut_unverified` no longer applies to them) — a deliberate change
to capital output, pinned by a test asserting a `SUPPORTED` thesis sizes larger than an
otherwise-identical `UNVERIFIED` one.

**2. The deterministic `_extract_macro_indicators(initial_answers)` remains the sole source of
`MacroIndicators` for `classify_regime`.** The container's macro read is the LLM's _qualitative_
Step-2 reading (per-indicator favorable / unfavorable / missing), consumed **only** by the
`analysis.md` renderer — it never reaches `classify_regime`. Rejected: letting the model's
reported macro numbers feed the regime table (non-deterministic; violates evidence-only).

**3. `analysis.md` is rendered deterministically by the node from the container** (principle 4);
the LLM no longer free-authors it. `data/agents/agent_4.md` (and the relevant contract wording)
are updated to match — per contracts' own "where a prompt disagrees, fix the prompt."

### Consequences

Good: determinism preserved (regime/sizing never see model-reported numbers); `analysis.md`
reproducible and carries every dropped claim; supported theses are no longer over-haircut.
Bad / to watch: capital output shifts upward for supported theses (deliberate, tested); the
LLM emits `expected_value` that the post-processor recomputes from scenarios for the gate — a
pre-existing redundancy, out of S3 scope. The `AnalysisHalt` type is defined here as pure data
(no `TerminalState`); wave S5 owns the §6a routing and the `TerminalState` reconciliation and
merely consumes the presence of a halt — the boundary that avoids a conflicting second model.

### Confirmation

Tests: a `SUPPORTED` thesis sizes strictly larger than an identical `UNVERIFIED` one through
`size_position`; `classify_regime` is driven only by `_extract_macro_indicators` (the container's
`macro_read` is never passed to it); `analysis.md` is rendered from the container including
dropped-claim reasons.

## More Information

Restores `docs/architecture.md` §6.5. Related: `docs/pipeline_contracts.md` §4/§5/§6a/§8a/§9,
`docs/design_decisions.md` §2, `data/agents/agent_4.md`, and ADRs 0003/0004 (same
capability/determinism-preserving bar).
