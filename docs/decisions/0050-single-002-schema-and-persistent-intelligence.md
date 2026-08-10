# Single 0.0.2 schema and persistent intelligence contracts

## Context and problem statement

money-pit has not reached production. Its ordered migrations preserve obsolete run-local models and make a clean redesign look like a compatibility upgrade. The new workflow needs point-in-time claims, thesis revisions, research history, portfolio decisions, and execution outcomes to survive across runs without reading historical caches.

## Decision drivers

- Refuse accidental use of an old or manually changed database.
- Preserve every material financial judgment as immutable, point-in-time state.
- Keep source trust and provenance policy replayable after configuration changes.
- Prevent discovery snippets from becoming verification evidence.
- Keep capital-sensitive settings explicit and versioned.

## Considered options

- Continue the ordered migration chain and translate old state.
- Create a separate database with one release baseline and an exact schema identity.
- Use document files as the primary persistent state.

## Decision outcome

Chosen option: one fresh SQLite baseline for release 0.0.2 at `data/intelligence.sqlite3`.

Initialization applies the baseline only when the database has no application schema objects. A nonempty database must carry the expected application ID, release, and schema fingerprint, and its current `sqlite_schema` catalog must recompute to the same fingerprint. Any mismatch fails before application writes. There is no compatibility reader or cache importer.

Source definitions are immutable revisions identified by canonical validated-model hashes. Source items bind the revision active at discovery. Claims, resolution decisions, verification results, candidate hypotheses, thesis revisions, temporal contributions, research activity, decision snapshots, plans, execution events, and outcomes are append-only records with explicit `known_at` or capture times where point-in-time replay requires them.

`sources.toml`, `strategy.toml`, and `execution.toml` are separate strict documents. Strategy and execution limits have no production defaults. ADR 0063 supersedes this decision's credential-storage statement.

### Consequences

- Old database files and cached source material remain untouched and unread.
- Manual schema drift and databases from another build fail closed.
- Replay can recover the exact source policy and financial inputs used by a decision.
- Operators must supply complete strategy and execution values before the application can plan or trade.
- Changing the schema before a production release requires replacing this baseline and release identity, not appending compatibility migrations.

## Validation

Tests must cover initialization of a logically empty database, idempotent reopening, rejection of unknown metadata, rejection of catalog drift, source-definition revision history, point-in-time query cutoffs, and exact plan/execution bindings.
