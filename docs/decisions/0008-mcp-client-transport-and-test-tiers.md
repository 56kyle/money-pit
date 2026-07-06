# Alpaca integration: alpaca-py reads, MCP writes, and offline-default test tiers

- Status: accepted
- Date: 2026-07-06
- Deciders: owner, architecture author

## Context and Problem Statement

Phase 7 replaces the injected paper/no-op capital seams with real integrations. The architecture
(`architecture.md` §8/§11, `design_decisions.md` §7) specified the Alpaca read/write safety boundary
as **toolset scoping on the one official server** — a `market-data`-scoped read instance and a
`trading`-scoped write instance — so "only the execution stage physically holds an order tool."

Reading the installed `alpaca-mcp-server` 2.0.2 source at integration time invalidated three
assumptions that boundary rested on:

1. **There is no `place_order` tool.** `postOrder` is excluded and split into `place_stock_order` /
   `place_crypto_order` / `place_option_order`. Equity orders use `place_stock_order` (its params are a
   superset of our `ExecutionParameters`).
2. **Toolset names differ.** The real `ALPACA_TOOLSETS` names are `account, trading, stock-data,
   assets, …` — not `market-data`/`trading`. Paper/live is `ALPACA_PAPER_TRADE`; creds are
   `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`; the console script is `alpaca-mcp-server`, stdio by default.
3. **Live positions live in the `trading` toolset** — the same toolset that registers the
   order-placement tools. There is no read-only positions toolset, and Alpaca has no key-level
   read/write permission split. So any MCP instance scoped to read the portfolio snapshot's positions
   would **also physically hold `place_stock_order`** — the exact thing the boundary forbids.

The clean "toolset scoping is the structural boundary" claim therefore cannot hold for the
account/positions read the snapshot needs, only for genuinely-separate market-data reads.

A second problem: the owner's paper/live selection lives in `Config`, including under pytest. If the
default test suite connected to a live server it would become network-, credential-, and
order-dependent — breaking this repo's mock-free-**but-offline** ethos (ADR 0002) and the
"identical inputs → identical outputs" principle.

## Decision Drivers

- Preserve the real safety goal — only the execution stage can place an order — despite Alpaca having
  no key-level read/write split.
- Keep the default test suite fully offline and deterministic; make live connectivity a deliberate,
  opt-in act.
- Pin the committed artifacts (tool name, toolset names) to the real server, per the spec's own
  "read the official schema at integration time" instruction.
- Keep transport concerns out of the deterministic pipeline nodes (they stay behind the existing
  `PortfolioFetcher` / `OrderPlacer` seams).

## Decision Outcome

**1. Reads via the alpaca-py SDK; writes via MCP.** The portfolio snapshot's `PortfolioFetcher` reads
through the `alpaca-py` `TradingClient` (read calls only; `paper` from resolved credentials) in
`money_pit/alpaca_portfolio.py` — **not** MCP — so no positions-reading client ever holds an order
tool. The `OrderPlacer` is an MCP client (`mcp/clients.py`, `AlpacaWriteDeps`) scoped to
`ALPACA_TOOLSETS="trading"`, **connect-per-call** (spawn `alpaca-mcp-server` over stdio → call
`place_stock_order` → close, bridged to sync with `asyncio.run`), and is constructed only on the
production execution path. The boundary is now **client separation + only-execution-places-orders**,
documented honestly as such, rather than a toolset-scoping guarantee that Alpaca's API cannot provide.
A5 market-data research reads (a `stock-data`-scoped MCP instance) were deferred — no consumer needs
them yet, and the existing yfinance shim already satisfies `fetch_ticker_price`.

**2. Retarget the committed artifacts to the real tool taxonomy.** `compute/tool_map.py`'s
`ACTION_TYPE_TO_TOOL` targets `place_stock_order` (equities only; crypto/options out of scope for v0),
which auto-rekeys `pinned_manifest`. `live_manifest(credentials)` introspects the connected trading
server's tools into `{name: inputSchema}` and fails closed via `ManifestUnavailableError`; it is the
opt-in successor to the static `pinned_manifest` default (ADR 0004). A `money-pit pin-order-schema`
CLI command introspects `place_stock_order`'s `inputSchema` and writes the pinned
`alpaca_order_schema.json` (sentinel stripped) — the repeatable form of the ADR-0007 deployment pin.

**3. Credentials resolve through a typed, fail-closed resolver.** `resolve_alpaca_credentials` maps
the existing keyring identity (`api_key = alpaca_username`, `secret = keyring.get_password(
alpaca_service, alpaca_username)`) into a frozen `AlpacaCredentials`, deriving `paper` from a
`-paper`/`-live` service suffix and raising `CredentialResolutionError` on a missing secret or an
unrecognized suffix — it never guesses paper vs live.

**4. Offline-default suite + opt-in live tier.** The default suite never connects: pure helpers
(`_write_env`, `_order_arguments`, `_extract_order_id`, the credential resolver, the snapshot field
mapping) are unit-tested offline; the live-only stdio/SDK/SMTP boundaries carry `# pragma: no cover`.
A `tests/acceptance_tests/paper_trade/` tier marked `@pytest.mark.live` is deselected by default and
skips without creds; it is the only thing that connects to Alpaca paper (swap the service to `-live`
for the live smoke). `production_deps(config)` is never auto-invoked — a bare `run_pipeline()` keeps
failing closed via `_require_capital_critical_deps`, so production capital movement is always explicit.

### Consequences

Good: the committed artifacts match the real server; the safety goal holds via client separation; the
default suite stays offline and deterministic; live connectivity is a deliberate opt-in; paper/live is
a single typed decision.

Bad / to watch: the boundary is now a construction/usage discipline, not a toolset-scoping guarantee —
a future edit that constructs `AlpacaWriteDeps` outside the execution path would breach it silently
(no type prevents it). Connect-per-call spawns a subprocess per order (acceptable at N=1 volumes;
idempotent `client_order_id` covers retries). The `place_stock_order` params are strings and use `qty`
(not our `quantity`); the write client remaps `quantity→qty` as a marked transitional shim whose
terminus is the `ExecutionParameters` rename + float→string reconciliation at the pin run (behind the
ADR-0007 gate).

### Confirmation

Offline: `nox -s tests-python` green at 100% with the live tier deselected; the pure-helper and
fail-closed (`CredentialResolutionError`, `ManifestUnavailableError`) tests pass; `run_pipeline(
overrides=None)` still raises `MissingPipelineDependencyError`. Live (owner-run, opt-in):
`pytest -m live` against Alpaca paper fetches a snapshot and finds `place_stock_order` in the
introspected manifest.

## Considered Options (key rejections)

- **All-MCP with a `trading`-scoped read instance for the snapshot.** Rejected: that instance
  physically holds `place_stock_order`, breaking the very boundary being protected — worse than
  honestly separating clients.
- **Keep `place_order` as an internal alias, translate only at the MCP boundary.** Rejected: adds an
  indirection layer that live introspection and the pinned manifest would not reflect; the spec always
  intended the emitted keys to match the real tool literally.
- **Connect to an externally-running MCP server over HTTP.** Rejected: pushes server lifecycle and
  the paper/live selection out of the package, contradicting `package_structure.md` ("runs inside the
  pipeline process") and the owner's requirement that config own paper/live.
- **Live connections in the default test suite.** Rejected: network/credential-dependent, slow, and
  non-deterministic — breaks the offline ethos and reproducibility principle.

## More Information

Extends ADR 0002 (mock-free injected seams), ADR 0004 (static→live manifest), ADR 0007 (unpinned-schema
fail-closed gate). Related: `docs/architecture.md` §8/§11/§15 #1, `docs/design_decisions.md` §3/§7,
`docs/package_structure.md`, `src/money_pit/mcp/{clients.py,manifest.py}`,
`src/money_pit/alpaca_portfolio.py`, `src/money_pit/config.py`, `src/money_pit/__main__.py`.
