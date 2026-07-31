---
status: accepted
date: 2026-07-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Separate Content Identity from Source Acquisition

## Context and Problem Statement

An evidence asset used its content hash as `asset_id`, but the same database row also owned source-item, retrieval-time, and media-type fields. Fetching identical bytes from another item or source therefore looked like an asset identity collision and discarded the later acquisition provenance. Historical backfill also reused the operational sync cursor, so a bounded backfill could move the live synchronization position.

## Decision Drivers

- Identical bytes must have one immutable content identity and one stored payload.
- Every source acquisition must retain its own source item, retrieval time, and reported media type.
- Replaying the same fetched artifact must not duplicate durable rows or search fragments.
- Backfill must resume after a bounded or interrupted run without changing live sync state.
- Existing asset and sync-cursor data must migrate without losing provenance.

## Considered Options

- Keep acquisition fields on the asset and widen asset identity with source metadata.
- Preserve and restore the sync cursor around each backfill.
- Separate canonical assets from acquisitions and key cursors by purpose.

## Decision Outcome

Canonical `evidence_assets` contain only the SHA-256 identity, canonical local path, and asset metadata. `evidence_asset_acquisitions` records source item, retrieval time, and reported media type for each encounter. The acquisition composite key makes replaying the same fetched artifact idempotent while allowing the same bytes to retain provenance from other items, sources, or retrieval times.

Every resolved acquisition stores (source_item_id, content_version) and enforces that identity with the composite foreign key to source_items. Migration 0010 does not infer versions for earlier acquisition rows because a stable item identifier can have several versions. It preserves those rows as legacy_unresolved; they remain available for historical inspection but cannot satisfy a claim-provenance gate. A source ingestion batch writes item versions, assets, resolved acquisitions, fragments and FTS rows, and its cursor in one SQLite transaction after content-addressed bytes are stored. A failed database write can therefore leave an unindexed CAS payload, but it cannot advance the cursor or expose partial evidence state. Fragments remain content-owned and are validated on replay before reuse.

`source_cursors` uses `(source_id, cursor_purpose)` as its primary key. Normal sync reads and advances `sync`; backfill reads and advances `backfill`. A bounded backfill leaves its cursor for the next invocation, while a terminal page clears only the backfill cursor.

### Consequences

- Good, because content storage deduplicates independently of where bytes were acquired.
- Good, because later acquisitions are durable even when their fragments already exist.
- Good, because retrying an identical evidence document is a no-op.
- Good, because backfill cannot overwrite the operational sync position.
- Bad, because callers that query provenance must join assets to acquisitions.
- Bad, because historical acquisitions remain explicitly unresolved until they are re-fetched with an exact item version.
- Neutral, because a repeated retrieval at a new timestamp is a distinct encounter.

## Confirmation

Repository tests must demonstrate cross-item content deduplication, exact item-version foreign-key enforcement, acquisition retry idempotency, explicit unresolved legacy migration, all-or-nothing ingestion including cursor advancement and FTS indexing, independent sync/backfill cursors, and bounded backfill resumption.