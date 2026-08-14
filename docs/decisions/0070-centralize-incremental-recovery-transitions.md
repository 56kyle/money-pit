---
status: accepted
date: 2026-08-14
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Centralize incremental recovery transitions

## Context and Problem Statement

Schema 0.0.4 made intelligence work incremental, but research-wave lifecycle writes remained split between `PlannedResearchTaskStore` and `IntelligenceWorkRepository`. A wave also used one `run_id` for its immutable provider origin, later semantic completion, and checkpoint ownership. Retries under a different run could therefore collide with already-paid work. Inference usage correlation was ambient and optional, so a legitimate model call could reach durable accounting without a workflow identity.

## Decision Drivers

- Preserve downloaded evidence and every admitted model, search, fetch, and interpretation result.
- Make cross-run recovery idempotent without rewriting original provenance.
- Prevent model construction when durable state violates a recovery invariant.
- Attribute each transition and inference call to the run that performed it.
- Keep audits provider-free, credential-free, read-only, and safe on the operator database.

## Considered Options

- Continue repairing individual identity collisions at each caller.
- Reset active research state and repeat provider work under a new run.
- Centralize lifecycle transitions and separate semantic identity, origin provenance, and current ownership.

## Decision Outcome

Chosen option: `IntelligenceWorkRepository` is the sole persistence owner for incremental bundle, discovery, research-wave, checkpoint, and synthesis lifecycle state. `PlannedResearchTaskStore` owns planned tasks only.

A research wave has the semantic identity `(job_id, wave_number)`. `record_provider_wave` creates the immutable provider result and its original run. `complete_provider_wave` adds semantic interpretation output and records the completion run. `checkpoint_completed_wave` accepts only the stable wave identity, derives cumulative limits from stored deltas, creates the checkpoint, records the checkpoint run, and consumes the wave atomically. There is no insert-on-completion fallback.

Schema 0.0.5 retains the legacy `run_id` column as provider-origin storage for catalog compatibility and exposes it as `origin_run_id` in typed code. It adds `execution_completed_run_id`, `execution_completed_at`, and `checkpointed_run_id`. The exact 0.0.4 migration backfills existing execution and checkpoint ownership from the legacy origin without modifying any evidence, interpretation, research, usage, or artifact row.

Every A1–A4 agent invocation requires an `InferenceInvocationContext(run_id, work_unit_id)`. Production factories require an `InferenceUsageSink`; tests and nondurable tools must opt into `NullInferenceUsageSink`. Ambient context variables and optional correlation are removed.

`money-pit intelligence audit` opens the current database read-only and classifies durable state as `healthy`, `resumable`, or `blocked`. Provider-completed waves, execution-completed waves, and expired claims are resumable. Malformed transition ownership, checkpoint counter drift, malformed inference correlation, or invalid artifact bindings are blocked. `intelligence update` runs the same audit after schema initialization and before configuration secrets or providers are constructed.

## Consequences

- A retry run can complete or checkpoint an earlier provider wave without changing its origin.
- Research budgets are derived from immutable wave deltas rather than resupplied caller counters.
- Missing inference correlation is a type and call-contract failure before durable accounting.
- Operator updates fail locally before provider use when recovery state is blocked.
- The migration accepts only the exact 0.0.4 fingerprint; unknown or drifted databases remain unchanged.

## Confirmation

- Failure-injection tests cross run boundaries at provider, semantic completion, and checkpoint transitions.
- Migration tests cover provider-completed and execution-completed predecessor waves, rollback, and reopening.
- A copied operator database must migrate and pass the provider-free audit before the original database is migrated.
- Ruff, basedpyright, pytest, Nox, documentation, build/install smoke tests, pre-commit, and `git diff --check` must pass.
