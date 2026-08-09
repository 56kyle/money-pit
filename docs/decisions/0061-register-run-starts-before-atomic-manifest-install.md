---
status: accepted
date: 2026-08-09
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Register run starts before atomic manifest installation

## Context and Problem Statement

A run start must be durable in both SQLite and `data/runs/<run-id>/run.json` before any stage side
effect begins. SQLite and the filesystem cannot share a transaction. Installing the final directory
before inserting SQLite leaves an unregistered run after a database failure, while inserting SQLite
without a recoverable filesystem step can leave an invisible run. A completion timestamp on the
immutable start record also cannot represent a lifecycle transition without mutation.

## Decision Drivers

- SQLite is the authority for registered run starts.
- The filesystem manifest remains an immutable, independently inspectable projection.
- A failure at either persistence boundary must be detectable and safely retryable.
- Reconciliation must never overwrite a differing manifest or database record.
- Actual completion and failure times must not be backdated.

## Considered Options

- Install the final filesystem directory and then register SQLite.
- Register only in SQLite and generate manifests on demand.
- Coordinate a pending manifest, SQLite registration, and atomic directory install.

## Decision Outcome

Generate one canonical UUID4 and write its deterministic `RunRecord` to
`data/runs/.run-<run-id>.pending/run.json`. Fsync the temporary manifest, atomically install it within
the pending directory, append the exact immutable SQLite start row, and then atomically rename the
pending directory to `data/runs/<run-id>`. Stage side effects begin only after the final install.

Registration is idempotent and reconciles typed semantic equality. SQLite plus a pending manifest
completes the final install. SQLite alone reconstructs the exact pending manifest and installs it.
A final manifest alone registers its exact record. A pending manifest alone resumes registration.
Final and pending manifests may be collapsed only when both equal the requested record. Any
malformed or differing state fails closed without rewriting durable evidence.

`RunRecord` represents only the immutable start. A separate `RunTerminalEvent` records exactly one
`completed` or `failed` outcome. Failed events contain a stable bounded `failure_kind` and structured
recovery detail only; they never persist exception text, transcripts, provider bodies, or secrets.
Absence of a terminal event means interrupted or still running.

### Consequences

- Good, because every stage starts from a database-registered and visibly installed run.
- Good, because retries recover every one-sided persistence state without guessing identity.
- Good, because lifecycle history remains append-only.
- Bad, because registration needs an explicit reconciliation state machine across two stores.
- Neutral, because a valid pending directory may remain after a boundary failure until retry.

## Pros and Cons of the Options

### Install filesystem first

- Good, because the run has an artifact directory immediately.
- Bad, because a database failure leaves the filesystem looking complete but unregistered.

### SQLite-only registration

- Good, because there is one transactional persistence boundary.
- Bad, because copied artifacts lose their durable identity and operators cannot inspect a run
  independently of SQLite.

### Pending manifest, SQLite registration, atomic install

- Good, because incomplete states are explicit and retryable.
- Good, because final-directory presence means both persistence steps completed.
- Bad, because exact typed equality must be checked during every reconciliation.

## More Information

This decision replaces the removed generic-run ADR and extends the SQLite ownership decision in
[ADR 0039](0039-sqlite-index-and-content-addressed-assets.md).
