---
status: accepted
date: 2026-08-14
decision-makers: [Kyle Oliver]
consulted: []
informed: []
supersedes: ["ADR-0068 candidate, research, and synthesis identity"]
---

# Separate semantic variants from durable work state

## Context and Problem Statement

The incremental workflow originally used an immutable A2 candidate identifier as the identity for research and synthesis. Candidate wording, discovery provenance, pending-task subsets, and research-session history could therefore create different durable identities for the same economic work. The inverse error was also possible: a broad deterministic normalization could merge proposals whose economic meaning was not proven equal.

The live history exposed both failures. Task sets for one GDX proposal progressed through zero, one, and three tasks as queue state changed. Several synthesis units then represented operationally different histories of substantially overlapping material. A deterministic pipeline cannot be trusted when it deterministically preserves the wrong semantic boundary.

## Decision Drivers

- Preserve every proposal, source origin, evidence item, provider result, interpretation, task, usage record, and completed output.
- Keep exact proposal meaning distinct from canonical equivalence and operator decisions.
- Permit automatic equivalence only when all structured semantic fields match exactly.
- Prevent unresolved capital references and ambiguous candidates from entering runnable research or synthesis.
- Make research task completion local to one versioned case while retaining exact reuse provenance.
- Invoke synthesis only when decision-relevant evidence state changes and passes a deterministic evidence gate.
- Migrate only the exact verified predecessor and fail before provider construction when semantic state is invalid.

## Considered Options

- Continue using candidate proposal identifiers and repair individual duplicate jobs or units.
- Use one fuzzy normalized hypothesis key and merge similar candidates automatically.
- Separate immutable proposals, exact semantic variants, canonical hypothesis groups, research cases, and synthesis material state.

## Decision Outcome

Chosen option: introduce separate durable identities for proposal provenance, exact semantic meaning, canonical equivalence, research progression, and synthesis eligibility.

Each immutable A2 proposal maps to one `HypothesisVariant`. Its identity contains the normalized capital reference, direction, horizon, theme, causal mechanisms, and regime assumptions. Prose subject, discovery origins, timestamps, and lifecycle state are excluded. A resolved instrument, instrument reference, or layered universe reference can provide capital identity. A proposal without one remains candidate-specific and unavailable. A theme alone cannot provide capital identity.

An exact variant starts in its own `CanonicalHypothesisGroup`. Another proposal joins that group automatically only when its complete variant identity is equal. The group identity is derived from the policy version and its sorted variant identities, so operator decision order cannot change the eventual semantic identity. A coarse match can create a versioned `HypothesisReview`, but it does not merge work. The review record remains immutable. `money-pit intelligence resolve` adds one immutable resolution record with actor, reason, decision, and time. A `same` decision creates a new successor group containing the prior groups' variants. It does not rewrite proposal memberships, mutate historical cases, fabricate a research job, or use one variant as the group authority. Provider-free reconciliation can subsequently construct a typed successor case from the exact union of predecessor obligations. A `distinct` decision remains durable and prevents a conflicting later merge.

Research uses one versioned case for a canonical group and exact source scope. Research scope, case, task, and premise fingerprints come from one storage-independent semantic identity module used by both migration and runtime admission. The material premise contains canonical claim state and the complete immutable A2 task set. It excludes queue status, claimant identity, timestamps, and planner progress. Initial tasks and A3 follow-up tasks have job-scoped obligation and execution records. A follow-up advances its existing job and does not change premise identity. A successor job can reuse an exact completed execution while retaining the source job and task identities. Only one current head can own runnable work for a case.

Synthesis first persists a typed material-state assessment. Migration and runtime use the same pure material projection. Its identity contains the canonical group, grounded observations, current material claim state, accepted evidence and verification state, contradictions, prior thesis revision, and synthesis policy version. Run, job, session, query order, planner text, failures, timestamps, and retries are operational provenance only. A4 runs only when the assessment records qualifying evidence or a decisive contradiction. Otherwise the system records `insufficient_evidence`, creates no A4 call, and keeps the hypothesis non-investable. Legacy history that cannot reconstruct the complete decision state remains `unavailable`; historical success flags alone never grant eligibility.

Incremental research reaches a terminal state through one atomic storage transition that admits its material assessment, material origin, and optional A4 obligation before changing the raw job to terminal. The semantic synthesis disposition `current` identifies the authoritative unit for a material state; the raw synthesis-unit status separately determines whether that unit is pending, claimable, or completed. A completed thesis revision is portfolio-eligible only through a current semantic binding, a completed raw unit, an eligible material state, and the exact namespaced output binding.

Schema 0.0.6 adds variants, canonical groups, append-only reviews and resolutions, group supersession, job-scoped task lineage, research succession, synthesis material state, and semantic dispositions. Initialization migrates only an exact 0.0.5 catalog in one transaction. The migration preserves existing rows, reconstructs provable exact identities, keeps one deterministic research head, and marks redundant historical work non-runnable. Ambiguous equivalence remains open for operator review. Unknown or drifted catalogs roll back without metadata or application-row changes.

Provider-free inspection is part of the storage contract. Status distinguishes actionable, needs-review, unavailable, and superseded work. Audit blocks provider construction only for invariant violations. Review listing and namespace-qualified lineage inspection read SQLite without configuration secrets or providers. Lineage exposes proposal membership, variants, groups, discovery origins, job succession, task execution and reuse, eligibility, synthesis output, and thesis revisions.

### Consequences

- Good, because deterministic hashes now represent one explicit semantic layer.
- Good, because ambiguous similarity cannot silently combine economic hypotheses.
- Good, because completed research can be reused without making global task status authoritative.
- Good, because operational history alone cannot enqueue another synthesis call.
- Good, because insufficient evidence consumes no A4 tokens and grants no capital authority.
- Bad, because operators must resolve some duplicate reviews before affected work can continue.
- Bad, because migration and lineage storage are more complex than one candidate-keyed queue.
- Neutral, because retained redundant rows remain visible audit history and do not require deletion.

## Pros and Cons of the Options

### Repair candidate-keyed work in place

- Good, because it requires the smallest schema change.
- Bad, because wording, provenance, task lifecycle, and material state remain one identity boundary.
- Bad, because each new recovery defect requires another local exception.

### Merge by one fuzzy normalized key

- Good, because similar proposals can share work without operator action.
- Bad, because normalization cannot prove equal horizon, mechanism, regime, or capital exposure.
- Bad, because a deterministic false merge repeats the wrong decision on every run.

### Separate variants, groups, cases, and material state

- Good, because automatic linking requires exact structured equality.
- Good, because append-only decisions preserve both original meaning and later equivalence.
- Good, because research and synthesis identities exclude mutable operational state.
- Bad, because callers and migration code must maintain explicit lineage between more records.

## Confirmation

Tests must pin exact variant linking, unavailable unclassified proposals, review policy versioning, append-only and transitive resolutions, incompatible `same` rejection, and source-filtered review listing. Real-SQLite tests must cover job task expansion, incomparable predecessor unions, exact execution reuse, one runnable head, synthesis material deduplication, insufficient-evidence closure, portfolio exclusion, and namespace-qualified lineage.

Migration tests must use the populated six-version and 25-asset predecessor shape. They must verify row and immutable-payload preservation, deterministic GDX and other historical reconciliation, idempotent reopening, transaction rollback, and rejection of unknown or drifted catalogs.

Rollout must remain provider-free until storage is verified:

1. Create and checksum a backup of `data/intelligence.sqlite3`.
2. Migrate a copy of the database.
3. Compare preserved row counts and immutable payload hashes.
4. Run `money-pit intelligence audit`, `status`, `reviews`, and `lineage` on the copy.
5. Migrate the live database only after the copied database is healthy.
6. Resolve ambiguous reviews before affected work continues.
7. Run one source-scoped research iteration and inspect its audit, lineage, run report, and usage before further updates.

Ruff, basedpyright, the full pytest suite, Nox, documentation, build and install smoke tests, pre-commit, and `git diff --check` must pass.
