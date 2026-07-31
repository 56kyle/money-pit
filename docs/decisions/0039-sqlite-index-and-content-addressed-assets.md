---
status: accepted
date: 2026-07-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Use SQLite as the Durable Index and Full-SHA-256 Files as the Asset Store

## Context and Problem Statement

The existing pipeline stores state inside timestamp-named run directories. That makes one run
inspectable, but it cannot efficiently relate evidence, claims, theses, plans, and outcomes across
runs. The new platform needs durable local state without introducing an operated database service,
and it must retain large source artifacts without putting opaque blobs in the index.

## Decision Drivers

- The application remains local and single-user.
- Schema changes must be explicit, ordered, and reproducible.
- Existing state must fail loudly if a previously applied migration changes.
- Concurrent scheduler and CLI processes must wait for bounded periods rather than fail immediately.
- Evidence bytes must be immutable, deduplicated, and independently inspectable.
- The storage boundary must enforce foreign keys and verify the JSON and full-text features it uses.

## Considered Options

- SQLite index with content-addressed filesystem assets
- JSON indexes alongside each run
- A client/server relational database
- Store all content as SQLite blobs

## Decision Outcome

Use the standard-library `sqlite3` module for the cross-run index and a filesystem
content-addressed store for source bytes.

Every database connection enables foreign-key enforcement, configures a bounded busy timeout, and
checks JSON and FTS5 behavior before use. `Database.transaction` is the single transaction choke
point. It opens one connection, begins an explicit deferred or immediate transaction, and commits,
rolls back, and closes it at that boundary.

Packaged SQL resources use contiguous numeric prefixes. The application records each migration's
name and SHA-256 checksum in `schema_migrations`. An applied history must be an exact prefix of the
packaged history; changed checksums, renamed versions, gaps, and database versions unknown to the
application are errors.

Assets are written under `assets/<first-two-hex>/<full-sha256>`. A temporary file is flushed and
atomically replaced into the digest path. An existing digest path is read and checked against the
full digest before reuse.

### Consequences

- Good, because the index supports transactions, foreign keys, JSON fields, and full-text search
  without an external service.
- Good, because immutable source bytes are deduplicated and remain normal files.
- Good, because migration drift is detected before later writes can compound it.
- Bad, because SQLite still permits only one writer at a time. The busy timeout reduces contention
  failures but does not make long write transactions acceptable.
- Bad, because database rows and asset files cannot participate in one atomic transaction. Callers
  must write the immutable asset before indexing it and tolerate unreferenced assets.
- Neutral, because migration 0001 establishes the original foundation tables and migration 0002
  adds the platform authority tables. Both are append-only history and must not be squashed after
  use.

## Pros and Cons of the Options

### SQLite index with content-addressed filesystem assets

- Good, because it fits the local deployment and supports relational projections.
- Good, because large media does not inflate the database or its write lock.
- Bad, because cross-store garbage collection must be explicit.

### JSON indexes alongside each run

- Good, because every run is self-contained.
- Bad, because cross-run queries require scanning and reconciling mutable files.
- Bad, because concurrent updates have no relational transaction boundary.

### Client/server relational database

- Good, because it supports multiple writers and richer operations.
- Bad, because it adds deployment, credentials, backup, and availability obligations that the
  single-user application does not need.

### Store all content as SQLite blobs

- Good, because metadata and bytes share one transaction.
- Bad, because large video and document assets make routine database operations and backups heavy.

## More Information

The generic run layout and read-only legacy bridge are defined by
[ADR 0040](0040-generic-run-manifests-and-read-only-legacy-index.md).
