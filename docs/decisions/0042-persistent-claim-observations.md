---
status: accepted
date: 2026-07-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Preserve Immutable Claim Observations and Derive Current Claim Projections

## Context and Problem Statement

`AggregatedSignals` is a correct snapshot for one legacy run, but it cannot safely become long-lived
memory. Updating that object in place would erase when a source made a statement, which evidence
supported it, and whether a later source corrected or superseded it. Portfolio decisions also need
to distinguish source interpretation, factual verification, investment judgment, and allocation.

## Decision Drivers

- Point-in-time replay must never observe a later correction or verification.
- A correction must not destroy the original statement.
- “Not readily found” must remain distinct from contradiction.
- Verification evidence must remain independent of the originating source.
- Theses must be reviewable separately from factual claims and portfolio plans.

## Considered Options

- Mutate one canonical claim record as evidence changes.
- Store only immutable observations and reconstruct all current state on every read.
- Store immutable observations and verification results, with a derived canonical projection.

## Decision Outcome

Persist `ClaimObservation` as the immutable record of what one source asserted. The append boundary accepts an observation only when its source item already exists and every referenced evidence fragment has a resolved acquisition from that source item. Explicitly unresolved legacy acquisitions do not satisfy this gate. It carries a stable
canonical key, claim kind, subjects and instruments, source and evidence identifiers, assertion and
validity times, expiry, and an optional superseded-observation link. `asserted_at` is the source's
event time; `recorded_at` is the system-observed time and controls when the observation becomes
visible to point-in-time replay.

Persist every `VerificationResult` independently. Supporting and contradicting evidence identifiers must already identify durable evidence fragments; missing identifiers fail the append transaction. Its status is one of `supported`,
`contradicted`, `mixed`, or `unresolved`; supporting and contradicting evidence are separate
collections. Verification expiry and verifier version are explicit so stale checks can be
scheduled again without rewriting history. Verification results likewise retain `checked_at` as
event time and use `recorded_at` as the replay visibility boundary. Migration 0008 assigns one
conservative migration-time boundary to legacy rows because their original system-observed time is
not reconstructable; replay therefore never claims the system knew those rows before migration.

Derive `CanonicalClaim` as a replaceable projection containing current status, active observation
identifiers, last material change, and next refresh time. Historical replay is a pure read and never
changes materialized current state. Full current refreshes atomically replace the projection set at
one boundary so rows from different epochs cannot mix. The projection is disposable: immutable
observations and verification results remain the source of truth.

Persist `Thesis` as a distinct investment-judgment record linked to supporting and contradicting
claim keys. A thesis has exactly one instrument or theme, a lifecycle status, explicit invalidation
rules, and a normalized scenario distribution. It does not carry target weights. Deterministic
portfolio planning consumes active theses later.

### Consequences

- Good, because corrections, reversals, late evidence, and out-of-order arrivals remain replayable.
- Good, because backdated evidence cannot leak into a replay before the system recorded it.
- Good, because unresolved research cannot be confused with a false statement.
- Good, because claim verification cannot directly become an order.
- Good, because claims cannot cite evidence that lacks resolved acquisition provenance from their asserted source.
- Bad, because current-state reads require maintaining or rebuilding a projection.
- Bad, because canonical-key generation and observation deduplication become important services
  whose policies must be versioned separately.

### Confirmation

Tests must prove observations and verification results are frozen and extra-forbid, supersession
appends rather than mutates, canonical projections can be rebuilt deterministically, expired
verification is excluded from current support, and point-in-time replay excludes observations and
verification results recorded after the replay instant, including records with backdated event
times.

## Pros and Cons of the Options

### Mutable canonical record

- Good, because current reads are simple.
- Bad, because it destroys decision history and makes point-in-time replay unreliable.

### Event-only reconstruction

- Good, because there is only one source of truth.
- Bad, because every operator report and refresh scan must replay all claim history.

### Immutable records with a derived projection

- Good, because it keeps complete history and efficient current reads.
- Good, because a projection can be discarded and rebuilt after schema or policy changes.
- Bad, because write paths must update two storage concerns atomically.

## More Information

This decision replaces cross-run mutation of `AggregatedSignals`; it does not remove the legacy
run artifact while the compatibility path exists. Storage transactions and refresh scheduling are
defined by their own implementation decisions.
