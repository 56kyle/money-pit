---
status: accepted
date: 2026-07-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Use a Versioned Source Registry and a Three-Step Connector Lifecycle

## Context and Problem Statement

The original pipeline binds ingestion to one YouTube channel and stores normalized source output
inside a run directory. That shape cannot represent multiple independent feeds, durable discovery
cursors, edited source items, or evidence that remains useful after the run that first observed it.
It also couples network transport, source discovery, extraction, and downstream interpretation.

The platform needs one boundary for local files, web pages, feeds, video, email, and filings without
giving untrusted source content access to portfolio execution capabilities.

## Decision Drivers

- Source identity and content version must be stable and idempotent.
- Discovery, retrieval, and extraction failures must be independently visible.
- Every connector must apply byte, MIME, and timeout bounds before interpretation.
- Configuration must reject unknown fields, duplicate source identifiers, and unknown adapters.
- Adding a connector must not require changing the registry loader.
- Importing the package must never perform network I/O.

## Considered Options

- Keep one pipeline adapter per source and configure it in application settings.
- Use entry-point plugins with adapter-owned configuration.
- Use a versioned `sources.toml`, an explicit adapter registry, and a shared connector protocol.

## Decision Outcome

Use a versioned `sources.toml` document containing frozen `SourceDefinition` records. Version 1 is
loaded with `tomllib` and validated with an extra-forbid Pydantic model. The loader rejects duplicate
`source_id` values and requires every `adapter_name` to exist in an explicitly composed
`AdapterRegistry`.

Connectors implement three synchronous, transport-neutral operations:

1. `discover(cursor)` returns a bounded `DiscoveryBatch` of stable `SourceItem` versions.
2. `fetch(item)` returns a bounded `RawArtifact` with its SHA-256 digest.
3. `extract(artifact)` returns an `EvidenceDocument` whose fragments point back to the asset.

Network-backed connectors depend on an injectable `HttpTransport`. The standard-library transport
enforces timeouts, streaming byte limits, media-type validation, and public HTTP(S) URL validation.
No network operation runs during module import. Connector construction validates configuration but
retrieval happens only through `discover` or `fetch`.

Bundled factories cover UTF-8 text, audio assets, PDF assets, RFC 5322 email, manual webpages,
RSS/Atom, YouTube uploads-playlist discovery, and SEC-hosted filing feeds. Binary audio and PDF
connectors retain the original asset without inventing extracted prose. Transcription, PDF parsing,
and frame enrichment can append evidence later through dedicated processors.

### Consequences

- Good, because all source classes share one explicit lifecycle and typed failure boundary.
- Good, because cursor persistence and asset persistence can be implemented independently of a
  connector.
- Good, because connectors receive no broker or execution dependency.
- Bad, because a source that changes between discovery and fetch must be retried as a new version.
- Bad, because RSS cursor behavior assumes newest-first feeds and stops at the last seen item.
- Neutral, because YouTube discovery requires a runtime API-key environment variable; the registry
  stores only its variable name.

### Confirmation

Contract tests must pin strict registry decoding, duplicate and unknown adapter rejection, bounded
file and HTTP reads, MIME rejection, cursor behavior, content-version hashing, and the absence of
network activity at import time. Each connector must also pass a shared discover/fetch/extract
contract suite using a real temporary file or an injected in-memory transport.

## Pros and Cons of the Options

### Pipeline-specific adapters

- Good, because it preserves the current orchestration.
- Bad, because every new source repeats cursor, validation, and provenance behavior.
- Bad, because evidence remains owned by a transient run.

### Entry-point plugins

- Good, because third parties can install adapters without editing this repository.
- Bad, because plugin discovery and dependency isolation add machinery before an external plugin
  ecosystem exists.
- Bad, because adapter configuration becomes difficult to validate as one document.

### Versioned registry and explicit adapter composition

- Good, because the schema is inspectable, deterministic, and local.
- Good, because explicit composition makes available capabilities visible in code and tests.
- Bad, because adding a built-in adapter requires one registry-composition edit.

## More Information

This decision introduces the source boundary only. Durable cursor and evidence persistence belongs
to the SQLite storage decision, and legacy `data/daily_show/` import remains a separate compatibility
concern.
