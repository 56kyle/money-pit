---
status: accepted
date: 2026-08-14
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Ground synthesis in candidate-specific claim semantics

## Context and Problem Statement

Incremental A2 work is one discovery unit, but one unit can contain many observations and produce several unrelated candidates. A3 previously copied every observation from every candidate origin into each terminal candidate's A4 unit. A4 then searched up to twenty full observations for every inherited subject. Common words in the full-text OR query admitted unrelated history, while fragment identifier arrays made individual observation records tens of thousands of characters. One candidate could therefore exceed the inference budget before A4 without receiving any useful additional semantics.

## Decision Drivers

- A4 must inspect only evidence that grounds its candidate.
- Existing paid A2 and A3 results must remain reusable.
- Fragment provenance must remain durable without becoming purposeless model input.
- Claim resolution must retain enough candidates to recognize same, update, and contradiction relationships.
- The existing pre-provider budget guard must remain fail-closed.

## Considered Options

- Increase the A4 character budget.
- Ask A2 to emit observation identifiers and rerun historical discovery.
- Persist another candidate-observation table.
- Derive grounding from immutable candidate authority and typed observations.

## Decision Outcome

Derive candidate grounding deterministically from existing immutable records. A claim-grounded candidate receives only active observations from its cited canonical claims. A universe-grounded candidate receives only observations whose structured instrument or theme exactly matches its authorized universe reference. A source discovery unit that contains observations may not admit a candidate with no exact grounding. Configuration-only universe work may legitimately have no source observation.

The same derivation runs when A3 creates synthesis work and again when A4 reclaims an older unit. Recalculation lets existing paid A2 and A3 work resume without schema migration or model replay. It is not a new mutable identity: candidate basis, discovery-unit observations, and canonical projection membership are already durable authorities. New synthesis payloads store only the derived subset; older broad payloads are narrowed before inference.

Resolution retrieval removes common stopwords, searches a bounded local pool, and admits a candidate only when it shares at least two content tokens, has identical normalized claim text, or shares an exact structured instrument/theme. The configured result limit still applies after filtering.

A4 receives a compact typed observation containing claim semantics, timing, source item identity, structured references, causal assumptions, and supersession. Fragment identifiers remain in `claim_observations` for deterministic admission and audit but are omitted from model input because the model cannot dereference them. This refines ADR 0055: an atomic model record is the complete semantic projection required for the decision, not necessarily the complete persistence representation.

Research-planner material-anchor keys remain inside the typed research assessment. They are local verification-checklist identities and are not assumed to be canonical-claim keys. A4 requires canonical claim projections only for keys explicitly cited by the candidate's discovery basis. This preserves claim-grounded authority without inventing canonical claims for universe-grounded research anchors.

The character budget remains unchanged. A resolution subject and its filtered candidates remain indivisible and still fail locally if the compact semantic unit cannot fit.

### Consequences

- One source bundle can safely produce multiple candidates without cross-contaminating A4.
- Existing active synthesis work is resumable and does not repeat A1 through A3.
- Resolution recall is deliberately bounded by deterministic lexical or structured relevance.
- Full provenance remains available to admission, replay, and audit outside model context.
- A genuinely oversized relevant resolution unit still stops before provider invocation.

## Confirmation

Tests reproduce common-word contamination, exact structured candidate grounding, large fragment provenance, and the indivisible budget guard. Pipeline, claim, research, and persistent incremental integration tests verify that compact input does not weaken deterministic materialization or recovery.
