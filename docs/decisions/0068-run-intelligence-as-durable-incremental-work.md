---
status: accepted
date: 2026-08-12
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Run intelligence as durable incremental work

## Context and Problem Statement

The original `money-pit run` command bound model work to one global A1 through A6 harness. A failed or interrupted run could repeat completed inference, while discovery and research could expand across the complete historical backlog. Per-request input limits and per-candidate research limits did not bound the total work in one run. The system needs durable work identities that survive run failure without deleting or rewriting the existing evidence and research record.

## Decision Drivers

- Every paid model call must advance an unfinished semantic work unit.
- Source-focused updates must not consume unrelated backlog.
- Completed and terminal work must remain reusable across later runs.
- Provider usage must be observable without storing prompts or mutable prices.
- Existing 0.0.3 history must be preserved through an exact fail-closed migration.
- Portfolio planning must remain separate from intelligence updates.

## Considered Options

- Retain run-scoped stages and add only per-run call limits.
- Keep the global harness but reduce prompt sizes and add resumable stage checkpoints.
- Introduce independent, fingerprinted work ledgers with bounded claims.

## Decision Outcome

Chosen option: introduce independent, fingerprinted work ledgers with bounded claims. Replace `money-pit run` with `money-pit intelligence update`. One update selects a bounded batch. A source selector restricts the complete update to work caused by that source. A global update uses a deterministic fair queue.

A1 groups the transcript and derived frames for one source-item version. A2 receives compact discovery signals instead of the complete claim history. Each A2 result, including an empty result, completes its durable inputs. A3 keys resumable jobs by candidate and material-premise fingerprint. One update processes at most two candidates and one focused wave for each candidate. Verification evidence remains scoped to its candidate unless an operator later promotes it for discovery. A4 persists each completed synthesis independently.

Schema release 0.0.4 adds interpretation bundle and chunk work, compact discovery units and batches, premise-versioned research jobs and checkpoints, canonical URI admission, per-candidate synthesis units, and sanitized inference-call accounting. A run claims bounded work; it does not own the work identity. The same run may resume its claim immediately. Another run may reclaim it only after the owning run records a terminal event or the explicit 30-minute claim lease expires. This prevents concurrent updates from duplicating paid inference while still recovering work after a hard process failure. Completed work is immutable.

Canonical URI equality is only the deduplication key, not reuse authority. Research re-fetches the
URI when discovery metadata cannot prove its content version, then reuses durable work only when the
content hash, content version, source-definition hash, evidence processor name and version, interpretation model,
and interpretation prompt version all match. The evidence bundle is reconstructed from durable
documents and fragments. A completed interpretation is reconstructed from its exact
model-and-prompt-bound interpreter attempt and admitted observations, so exact reuse contributes
candidate context without another model call. Any identity mismatch follows normal processing.

Each job, canonical-URI, and seven-field content-and-policy identity admission is immutable. Later occurrences
reconstruct durable evidence without provider I/O only when the latest job admission matches the
current source policy, processor name and version, interpretation model, and prompt version, and
every reconstructed asset has a completed exact interpretation. A policy mismatch or incomplete
interpretation follows normal fetch and processing, then appends a new identity-scoped admission.
Bundle reconstruction selects only assets with a successful attempt by the admitted processor
identity, preventing fragments from a different processor revision from entering an exact-reuse
result.

Inference accounting records model identity, request hash, timing, status, and provider-reported token usage. It never accepts or persists rendered prompts and does not calculate cost. `money-pit intelligence status` and `money-pit intelligence show` read this state without resolving credentials or constructing providers.

The migration accepts only the exact packaged 0.0.3 schema fingerprint. It creates the new ledgers and backfills durable completed interpretation, discovery, and candidate research history in the same transaction before recording the 0.0.4 identity. Because 0.0.3 interpretation attempts did not bind their model, migrated successes are preserved through an explicit legacy-coverage ledger rather than relabeled as current-policy matches. A1 may reuse their admitted observations and original attempt identities, while only uncovered assets receive a current-policy attempt. New work continues to require the exact prompt-and-model policy identity. Unknown or modified schemas remain fail-closed.

Portfolio review invokes A5 only. It does not use the incremental intelligence runner. Source synchronization remains a separate operation that schedulers perform before a bounded update.

### Consequences

- Updates can complete successfully while bounded work remains queued.
- Empty discovery results are durable completions and are not purchased again.
- Research progress is reusable after an enclosing run fails.
- Source-focused work does not drain the global queue.
- Operators can inspect progress and actual token use without invoking providers.
- More tables and explicit lifecycle transitions are required.
- Stage artifacts remain run summaries; work ledgers are the recovery authority.

## Pros and Cons of the Options

### Retain run-scoped stages and add limits

- Good, because it reduces the maximum cost of one invocation.
- Bad, because it stops waste instead of removing repeated and irrelevant work.

### Add checkpoints to global stage inputs

- Good, because completed stage fragments can survive an interruption.
- Bad, because unrelated history can still enter a source-focused run.

### Use fingerprinted work ledgers

- Good, because persistence, source scope, and bounded selection address repeated spend at its cause.
- Good, because actual usage supports later model and prompt calibration.
- Bad, because work identity, migration, and resume behavior add persistence complexity.

## Confirmation

Migration identity, rollback, and backfill tests verify exact predecessor acceptance, preservation of legacy rows, and rejection of modified schemas. Repository tests verify immutable collisions, bounded claims, crash reclamation, empty completions, source scoping, cumulative research safeguards, synthesis checkpointing, and sanitized usage aggregation. Pipeline and CLI tests must also prove bundled interpretation, compact discovery inputs, non-recursive verification evidence, deterministic research stop conditions, provider-free status and show commands, bounded update progress, and A5-only portfolio review. Ruff, basedpyright, pytest, Nox, documentation, build, pre-commit, and `git diff --check` must pass.
