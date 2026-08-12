---
status: accepted
date: 2026-08-12
decision-makers: [Kyle Oliver]
consulted: []
informed: []
supersedes: [ADR-0050 migration policy]
---

# Migrate the verified 0.0.2 schema in place

## Context and Problem Statement

The 0.0.2 partial unique index for successful claim interpretations omits `asset_id`. Two different
assets acquired for the same source item and content version therefore cannot both complete under
the same interpreter version. Existing databases can contain expensive acquired evidence and
partially completed intelligence, so replacing the baseline and rejecting those databases would
discard valid state.

ADR 0050 required every non-current database to fail closed and prohibited compatibility
migrations. This decision supersedes only that migration-policy portion. Its exact schema identity
and fail-closed requirements remain in force.

## Decision Drivers

- Preserve all rows in an exact 0.0.2 database.
- Do not recognize a database from metadata alone.
- Keep schema mutation and metadata mutation in one write transaction.
- Reject manual drift and every predecessor other than the retained 0.0.2 baseline.
- Let separate assets for one source item complete independently.

## Considered Options

- Reject 0.0.2 databases and require a new database.
- Rebuild the affected table and copy its rows.
- Migrate only the affected index after exact predecessor verification.

## Decision Outcome

Release 0.0.3 retains the packaged 0.0.2 baseline as the trusted predecessor definition and derives
its predecessor catalog fingerprint from that immutable package resource. A fresh database uses the
0.0.3 baseline, whose successful-interpretation identity is
`(source_item_id, content_version, asset_id, interpreter_version)`.

Initialization first reads schema metadata and independently fingerprints the complete live SQLite
catalog. A database is eligible for migration only when both values exactly equal the application
identity, release, and catalog fingerprint of the retained 0.0.2 baseline. Unknown releases,
metadata drift, and catalog drift fail before schema mutation.

For an eligible predecessor, initialization drops `claim_interpretation_success_idx`, creates its
0.0.3 definition, verifies that the resulting live catalog exactly equals the packaged 0.0.3
fingerprint, and only then updates the metadata release and fingerprint. `Database.initialize`
performs this work inside its existing `BEGIN IMMEDIATE` transaction. Any application, SQLite, or
verification failure rolls the entire migration back, including the index and metadata changes.

No table is rebuilt and no application row is inserted, updated, or deleted by the migration.
Future schema changes require their own explicitly bounded predecessor route; this decision does not
introduce a general best-effort migration chain.

### Consequences

- Good, because existing acquisitions, observations, theses, runs, and execution controls survive.
- Good, because multi-asset interpretation success has the same identity as retry scheduling.
- Good, because a forged release string cannot authorize migration without the exact catalog.
- Good, because interruption cannot expose a partially migrated schema.
- Bad, because the predecessor baseline remains packaged until 0.0.2 migration support is retired.
- Neutral, because source, strategy, and execution document versions do not change; their schemas
  and semantics are independent of this storage release.

## Pros and Cons of the Options

### Require a fresh database

- Good, because initialization remains simple.
- Bad, because it destroys continuity or requires an unsafe manual transfer of valid state.

### Rebuild the interpretation-attempt table

- Good, because it can change table constraints as well as indexes.
- Bad, because no table constraint must change and row copying adds avoidable integrity risk.

### Replace only the verified index

- Good, because it is the smallest data-preserving schema operation.
- Good, because before-and-after catalog fingerprints make the accepted states explicit.
- Bad, because release initialization now owns one narrowly supported migration path.

## Confirmation

Tests must pin the fresh 0.0.3 catalog, preservation of populated 0.0.2 rows, corrected index
columns, idempotent reopening, rollback on migration failure, and rejection of predecessor metadata
or catalog drift. Focused Ruff, basedpyright, and storage tests must pass.
