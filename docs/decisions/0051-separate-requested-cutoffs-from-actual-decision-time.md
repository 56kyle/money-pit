# Separate requested cutoffs from actual decision time

## Context and problem statement

A historical `as_of` cutoff was being reused as the timestamp of records created by the current run. That backdated agent interpretations and financial decisions, made new records appear historically available, and made replay unable to distinguish the historical baseline from the current run's delta.

## Decision drivers

- Point-in-time reads must not observe information that was unavailable at the requested cutoff.
- Current-run work must retain its actual creation and decision times.
- Later stages must be able to consume exact earlier-stage outputs from the same run.
- Portfolio decisions must bind every historical and same-run input used.

## Considered options

- Continue using one `as_of` timestamp for cutoff and record creation.
- Add an `originating_run_id` column to every intelligence table.
- Separate cutoff and actual times while using stage-artifact output IDs as authoritative run attribution.

## Decision outcome

Use `requested_as_of` only as the external-information cutoff. Runs persist actual `started_at` and `known_at`. Stage artifacts persist `requested_as_of`, actual `started_at`, optional `decision_at`, actual `known_at`, and exact input/output record IDs. Their canonical identity covers all timing, bindings, implementation version, and payload.

Repositories form a stage view by taking records with `known_at <= requested_as_of` and unioning exact output IDs attributed to the current run. They never rewrite the current run's `known_at` to the requested cutoff. This avoids adding redundant run columns to every append-only intelligence record.

Portfolio decision snapshots replace ambiguous `as_of` and `created_at` fields with `requested_as_of`, actual `decision_at`, and actual `known_at`. They also bind verification IDs, canonical projection hashes, the universe fingerprint, and processor, calibration, optimizer, and trade-generation versions. Execution eligibility remains an explicit validated decision property.

### Consequences

- Historical replay retains a strict information boundary.
- Same-run outputs remain available to downstream stages without becoming historical facts.
- Artifact and decision writers need a real clock and cannot derive actual timestamps from the requested cutoff.
- Harness repositories need exact-ID reads in addition to ordinary point-in-time reads.

## Validation

Tests must prove that later-known baseline records are hidden, exact same-run output IDs remain readable, artifact timestamps cannot be backdated, decision bindings round-trip, and historical runs cannot silently become executable.
