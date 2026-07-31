# Stepping-Stone Ledger

## Legacy `DAILY_SHOW_ROOT` Dual Layout

- **Introduced:** ADR 0040, 2026-07-29
- **Temporary shape:** Existing orchestration, recovery, stage execution, and scheduler code
  continues to read and write `data/daily_show/` through `DAILY_SHOW_ROOT`. New platform
  foundation code derives `data/assets/`, `data/runs/`, `data/reports/`, and the SQLite index from
  `RepositoryPaths`. The legacy importer reads and indexes `data/daily_show/` without modifying it.
- **Terminus:** All new pipeline runs are created through the generic run factory, recovery queries
  the durable run/execution index, and standalone stages resolve a run by manifest identity.
  Historical legacy artifacts remain in place and are accessed only through the compatibility
  index.
- **Done condition:** No production module outside `money_pit.legacy` imports
  `DAILY_SHOW_ROOT`; a point-in-time replay test covers an indexed legacy run; and removing the
  constant plus the dual-read branch leaves all offline gates green.

## Binary PDF and Audio Connector Retention

- **Introduced:** ADR 0041, 2026-07-29
- **Temporary shape:** PDF and audio connectors may retain validated source bytes in the asset
  store before dedicated parsing or transcription is available. They do not fabricate page,
  transcript, or frame evidence from file metadata.
- **Terminus:** Dedicated processors emit page fragments for PDFs and timestamped transcript and
  frame fragments for audio/video assets.
- **Done condition:** Provenance contract tests prove that every claim extracted from those source
  types links to the processor-produced page, transcript, or frame fragments.

## Inspect-Only Replay

- **Introduced:** ADR 0040 follow-up, 2026-07-29
- **Temporary shape:** `replay <run-id>` resolves a native manifest or read-only indexed legacy
  artifact snapshot and reports its location. It does not execute pipeline stages or orders.
- **Terminus:** Behavioral replay reconstructs decisions only from persisted point-in-time
  evidence, market, and portfolio snapshots while enforcing execution suppression.
- **Done condition:** Replay tests prove later evidence and prices cannot enter the reconstructed
  decision, execution cannot be enabled, and native and legacy snapshots produce stable results.

## Legacy Alpaca paper boolean

- **Introduced:** ADR 0046, 2026-07-29
- **Temporary shape:** `MONEY_PIT__ALPACA_PAPER` remains accepted by `Config` so existing
  installations do not silently change broker accounts. It is translated once into
  `BrokerEnvironment` at the credential boundary; execution authority uses `ExecutionMode`.
- **Terminus:** Configuration accepts only `MONEY_PIT__BROKER_ENVIRONMENT=paper|live` and no
  runtime model contains the paper/live choice as a boolean.
- **Done condition:** Migration diagnostics identify the old variable, configuration tests cover
  both named environments, and removing `alpaca_paper` leaves every supported deployment and
  offline gate green.

## Legacy recovery directory fallback

- **Introduced:** ADR 0046, 2026-07-29
- **Temporary shape:** Recovery may scan historical run journals when no durable execution-claim
  repository is supplied.

## Deprecated Credential Constructor

- **Introduced:** ADR 0046 compatibility follow-up, 2026-07-29
- **Temporary shape:** `AlpacaCredentials(paper=...)` translates the deprecated keyword into a stored `BrokerEnvironment`; conflicting forms are rejected.
- **Terminus:** All callers construct credentials with `broker_environment=`.
- **Done condition:** No supported caller uses `paper=`, and removing the custom constructor keeps all gates green.

## Explicitly Injected Legacy Write Authority

- **Introduced:** ADR 0046 compatibility follow-up, 2026-07-29
- **Temporary shape:** Approval-required low-level graph callers that explicitly inject an `OrderPlacer` retain the historical execution, recovery, fill-observation, and journal path. Normal production composition injects no writer. Autonomous graph execution remains disabled.
- **Terminus:** Every executable caller submits a durable `PortfolioPlan` through the guarded per-leg gateway.
- **Done condition:** No graph route treats dependency presence as production authority, and removing this branch leaves legacy control regression tests green.

## Disconnected Guarded Plan Executor

- **Introduced:** ADR 0046, 2026-07-29
- **Temporary shape:** Plan decisions, preflight validation, execution claims, and stable IDs exist, but `execute` stops before writer creation because no trusted live `AuthorizationContext` provider is connected.
- **Terminus:** A guarded executor refreshes trusted account, portfolio, market, order, and capability state before writer creation and every leg.
- **Done condition:** Approved plan executions use durable claims and plan-bound journals, failure injection covers every phase, and the CLI no longer has the intentional stop.
## Legacy `UrllibHttpTransport` Name

- **Introduced:** ADR 0048, 2026-07-29
- **Temporary shape:** Network-backed connectors retain the `UrllibHttpTransport` import name for
  compatibility, although its production implementation now uses address-pinned `http.client`
  connections and dnspython resolution.
- **Terminus:** Rename the transport and connector imports to `AddressPinnedHttpTransport`.
- **Done condition:** No production or test module imports `UrllibHttpTransport`, the compatibility
  alias is removed, and all source connector and HTTP boundary gates remain green.
