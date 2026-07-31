---
status: accepted
date: 2026-07-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Use Generic UUID4 Run Manifests and a Read-Only Legacy Index

## Context and Problem Statement

Run directories currently live under `data/daily_show/` and derive identity and ordering from a
timestamp-like slug. That name encodes one historical source and makes filesystem naming carry
temporal semantics. The portfolio intelligence platform needs runs for many trigger types while
preserving historical artifacts and the current pipeline during migration.

## Decision Drivers

- New run identity must not encode a source, stage, or creation time.
- Creation time must be explicit and timezone-aware.
- Run directories and manifests must be safe to retry and inspect.
- Existing `data/daily_show/` artifacts must not be renamed, rewritten, or decorated with import
  metadata.
- Legacy import must be idempotent and must report malformed or unavailable inputs explicitly.
- The current pipeline must continue to use `DAILY_SHOW_ROOT` until its consumers migrate together.

## Considered Options

- UUID4 directory names with an immutable manifest
- Timestamp directory names with a generic root
- A database-only run identity
- Move legacy directories into the new layout
- Read-only legacy indexing

## Decision Outcome

New runs use UUID4 directory names under `data/runs/`. Each directory contains `run.json` with a
schema version, UUID4 `run_id`, and explicit timezone-aware `created_at`. `RepositoryPaths` derives
the assets, runs, reports, database, and legacy locations from one data root without creating them.
The run factory creates a new directory and atomically installs its manifest.

The compatibility importer scans only immediate directories under `data/daily_show/`. It records a
stable path-derived legacy identifier, the legacy slug, an inferred creation instant when the old
format permits one, and an artifact-name inventory in SQLite. A unique legacy path makes repeated
imports idempotent. Every scanned directory receives a structured `indexed`,
`already_indexed`, or `skipped` diagnostic. The importer never writes inside the legacy tree.

`DAILY_SHOW_ROOT` remains available to the current pipeline as the explicitly recorded stepping
stone below. New foundation code uses `RepositoryPaths`.

### Consequences

- Good, because run identity is opaque and creation time is data rather than a filename convention.
- Good, because historical artifacts remain byte-for-byte and path-for-path intact.
- Good, because repeated indexing produces explicit diagnostics without duplicate rows.
- Bad, because the application temporarily has two run layouts and some consumers can see only one.
- Bad, because legacy creation time is best-effort and cannot be asserted when a slug does not
  follow the historical format.
- Neutral, because the importer inventories legacy artifact names but does not copy them into the
  content-addressed store.

## Pros and Cons of the Options

### UUID4 directory names with an immutable manifest

- Good, because identifiers are opaque and collision-resistant.
- Good, because time is validated independently.
- Bad, because operators cannot sort directory names to infer chronology.

### Timestamp directory names with a generic root

- Good, because directory listings are human-readable and chronological.
- Bad, because identity still carries temporal meaning and concurrent creation needs suffix rules.

### A database-only run identity

- Good, because there is one authority.
- Bad, because a copied run directory loses the information needed to identify itself.

### Move legacy directories into the new layout

- Good, because there would be one physical layout immediately.
- Bad, because it mutates historical evidence and can break existing recovery and stage workflows.

### Read-only legacy indexing

- Good, because it makes legacy state queryable without changing it.
- Bad, because dual-read behavior remains until the migration terminus.

## More Information

The storage and migration mechanics are defined by
[ADR 0039](0039-sqlite-index-and-content-addressed-assets.md).
