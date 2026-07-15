# Context

The `docs/architecture.md` and `docs/pipeline_contracts.md` docs are complete but no Python package structure exists beyond a handful of stub modules (`log.py`, `config.py`, `constants.py`, `__main__.py`). The goal is to lay out the full `src/money_pit/` directory tree — module names, one-line responsibilities, grouping rationale — before any implementation begins, so the build order and import graph are intentional from day one. (The tree below is now built out through Phase 7; the earlier `trade.py` stub was removed.)

---

# Proposed Package Structure

```
src/money_pit/
├── __init__.py              # (existing)
├── __main__.py              # (existing) CLI entry points via typer
├── log.py                   # (existing) loguru setup
├── config.py                # (existing) pydantic-settings Config + load_config
├── constants.py             # (existing) APP_NAME, paths, slug datetime format
├── contracts.py             # Cross-layer DI TypeAliases (cycle-free leaf, imports only schemas): ToolManifest, ThesisAgent, PortfolioFetcher, OrderPlacer, EmailSender, CorroborationAgent, ClaimQuestionsAgent, AnswerSynthesisAgent
├── alpaca_portfolio.py     # alpaca-py-backed PortfolioFetcher (reads snapshot via TradingClient, not MCP — ADR 0008); yfinance best-effort sector, v0-deferred factor_tags/overlaps; NonEquityPositionError
├── alpaca_orders.py        # alpaca-py-backed FillObserver: observes an order's real fill by client_order_id (reads via SDK — ADR 0008/0017); typed OrderNotYetVisibleError/FillObservationError
├── email_sender.py         # Gmail smtplib EmailSender for the pipeline (direct, not over MCP — ADR 0009); typed EmailSendError
├── prompt_loader.py        # Loads the packaged agent prompts from prompts/ (ADR 0012)
│
├── prompts/                # Packaged agent system prompts (agent_1..agent_N.md), loaded at import via prompt_loader (ADR 0012)
│   └── __init__.py
│
├── schemas/                 # All Pydantic data contracts — single import source of truth
│   ├── __init__.py
│   ├── enums.py             # Every canonical enum: SignalTier, ClaimCategory, ActionType, ExecutionPhase, TerminalState, RecoveryDecision (ADR 0018), â€¦
│   ├── provenance.py        # SourceRef, SourceType
│   ├── signal_draft.py      # SignalSetDraft (raw A1 LLM output — before ticker norm, date parsing, validation)
│   ├── signals.py           # Claim, SignalSet, CorroborationEntry, AggregatedSignals (contracts)
│   ├── aggregation_draft.py # ClaimRelations (aggregator thin LLM output — agree/disagree labels)
│   ├── questions.py         # Question, InitialQuestions, SignalSummary
│   ├── question_draft.py    # DraftQuestion (A2 LLM output — claim-specific questions only)
│   ├── answers.py           # Answer, InitialAnswers
│   ├── answer_draft.py      # AnswerDraft (A3 LLM output — open-ended question answers)
│   ├── fetch_result.py      # Typed deterministic-retrieval result (A3 known-param fetch — ADR 0011)
│   ├── macro.py             # MacroIndicators (five-indicator snapshot consumed by compute/regime.py; isolated to keep regime's import surface narrow)
    ├── analysis_draft.py    # AnalysisJudgment (LLM draft from A4 — input to post-processor)
│   ├── action_steps.py      # ActionStep, ExecutionParameters, ActionSteps (post-processor output / A5 input)
│   ├── validation_results.py  # ValidationStep, ActionStepsValidation, ValidationStatus
│   ├── determination.py     # Determination
│   ├── journal.py           # ExecutionJournalEntry, ExecutionJournal
│   ├── recovery.py          # PriorRunReconciliation, ReconciledOrder — recovery reconciliation result written to recovery.json (ADR 0018)
│   ├── fills.py             # FillObservation — typed order-fill observation (raw status, mapped phase, filled_qty/avg_price/realized_notional) — ADR 0017
│   └── portfolio.py         # Position, PortfolioSnapshot
│
├── graph/                   # LangGraph wiring only — zero business logic
│   ├── __init__.py
│   ├── state.py             # PipelineState TypedDict (control keys: slug, working_dir, completed_steps, terminal_state, run_has_actionable_content, validation_steps, determination, failed_steps, sub_agent_spawned, determination_reason, recovery_decision (ADR 0018)) + require_working_dir/require_slug/with_completed_step accessors + PipelineNode Protocol
│   ├── graph.py             # StateGraph assembly: add_node / add_edge / add_conditional_edges
│   └── edges.py             # Conditional edge functions: recovery_router (proceed/HALT → END; ADR 0018), signal_gate, terminal_state_router, determination_router (3-branch: execute/notify/finalize), post_notification_router (terminate/finalize)
│
├── pipeline/                # One module per LangGraph node; owns file I/O for its stage
│   ├── __init__.py
│   ├── orchestration.py     # Scheduler trigger, working-dir creation, slug assignment
│   ├── recovery.py          # Graph entry node: reconcile_prior_run + make_recovery_node — re-observes the prior run's potentially-open legs via the fill observer; HALTs+emails on a still-open order (double-exposure), notice+proceeds on an abnormal-but-settled prior run, else proceeds; writes recovery.json; no auto-unwind (ADR 0018)
│   ├── snapshot.py          # Calls Alpaca read MCP, writes portfolio_snapshot.json
│   ├── aggregator.py        # Merges SignalSets, re-IDs claims, computes run-level has_actionable_content
│   ├── questions.py         # A2 node: template emission, ID assignment, routing-table data_sources, file writes
│   ├── retrieval.py         # A3 node: deterministic known-param fetch, budget control, file writes
│   ├── analysis.py          # A4 node: calls agents/thesis_judgment + compute/ post-processor functions, writes action_steps.json
│   ├── validator.py         # A5: manifest parse, jsonschema checks, three-file write; consumes compute/tool_map.py for action_type lookup
│   ├── determination.py     # A6: recompute_determination decision node + finalizer node (writes determination.json/.md once); DeterminationParseError, map_execution_outcome (ADR 0006)
│   ├── execution.py         # Execution sub-agent: transactional loop, idempotent orders, journal, compensation
│   └── notification.py      # Notification sub-agent: email templating per terminal state, send_email call
│
├── agents/                  # Thin LLM cores (Pydantic AI); no deterministic logic, no file I/O
│   ├── __init__.py
│   ├── corroboration.py     # Aggregator's thin LLM pass: label agree/disagree on pre-clustered claim groups
│   ├── claim_questions.py   # A2 LLM core: claim-specific thesis-validation + invalidation questions only
│   ├── answer_synthesis.py  # A3 LLM core: open-ended Brave/EDGAR lookups + answer synthesis
│   ├── thesis_judgment.py   # A4 LLM core: claim disposition, thesis narratives, scenarios → AnalysisJudgment
│   └── research_tools.py    # DeterministicResearchTools / OpenEndedResearchTools Protocols (A3 tool injection; converges with mcp.clients ResearchDeps)
│
├── adapters/                # Source adapters — one module per source type; all emit SignalSet
│   ├── __init__.py
│   ├── base.py              # ABC every adapter must subclass: abstract process(payload) -> SignalSet
│   ├── video.py             # Narrated-video adapter: yt-dlp + WhisperX + PySceneDetect + OCR/VLM → A1
│   └── video_llm.py         # A1 LLM core: multimodal/text → SignalSetDraft (encapsulated inside video adapter)
│
├── compute/                 # Pure deterministic functions; no I/O, no LLM; exhaustively unit-testable
│   ├── __init__.py
│   ├── signal_flags.py      # requires_validation, has_actionable_content, ticker normalization, signal counts
│   ├── aggregation.py       # Claim union, run-global re-ID, tier max across corroborations
│   ├── factor_profile.py    # Factor-profile aggregation from per-position factor_tags
│   ├── regime.py            # Regime decision table: five indicators (yield curve, credit spreads, PMI, earnings revisions, inflation) → RegimeTag (UNCERTAIN on missing/conflict)
│   ├── sizing.py            # EV = ΣP×R, EV gate, constraint extraction, position sizing, clamps
│   ├── routing.py           # Category → tool routing table (consumed by A2 templating and A3 fetch)
│   ├── tool_map.py          # ActionType → MCP tool name + ActionType → compensating tool; shared by pipeline/validator.py, compute/execution_params.py, pipeline/execution.py
│   ├── confidence.py        # Confidence derivation from sources_used (primary/secondary/Brave rule)
│   ├── execution_params.py  # ActionType + judgment → execution_parameters with literal Alpaca MCP field names
│   └── fills.py             # Alpaca-status → ExecutionPhase mapping, terminal-status classification, execution-outcome derivation (ADR 0017)
│
├── mcp/                     # MCP client configuration; runs inside the pipeline process
│   ├── __init__.py
│   ├── constants.py         # PLACE_STOCK_ORDER_TOOL and related MCP tool-name constants (ADR 0008); imported by compute/tool_map.py and mcp/manifest.py
│   ├── clients.py           # AlpacaWriteDeps + make_alpaca_write_deps: connect-per-call OrderPlacer over a trading-scoped stdio alpaca-mcp-server (place_stock_order) + list_write_tools introspection (ADR 0008). AlpacaReadDeps/ResearchDeps dropped — no consumer; reads use alpaca_portfolio.py, edgar_search uses edgartools
│   ├── order_schema.py       # Shared loader: ALPACA_ORDER_SCHEMA_PATH + load_order_schema (fail-closed AlpacaOrderSchemaMissingError/MalformedError/NotPinnedError); single literal source for compute/execution_params.py and pipeline/validator.py
│   ├── manifest.py          # pinned_manifest(): static {<place_stock_order>: <pinned schema>} for A5 (ADR 0004); live_manifest(credentials): opt-in live introspection of the connected server (ADR 0008), fail-closed ManifestUnavailableError
│   └── alpaca_order_schema.json  # Pinned place_stock_order inputSchema snapshot (pinned from the live server via `money-pit pin-order-schema`, sentinel-free — ADR 0014); read via mcp/order_schema.py by compute/execution_params.py and pipeline/validator.py → tool manifest consumed by A5
│
├── ingestion/              # Video multimodal ingestion producer (orchestration-owned — ADR 0022); heavy libs lazy-imported, every stage an injected seam
│   ├── __init__.py
│   ├── artifacts.py        # Frozen intermediates: CaptionSegment, TranscriptResult, Keyframe, OnScreenExtraction, VideoArtifacts
│   ├── captions.py         # WebVTT parse + uploader→auto→whisper caption cascade (ADR 0019)
│   ├── transcribe.py       # faster-whisper transcription seam (ADR 0019)
│   ├── keyframes.py        # PySceneDetect scene-change keyframe selection (injected detector/extractor)
│   ├── on_screen.py        # Claude multimodal VLM on-screen text/attribution extractor (ADR 0020)
│   ├── fetch.py            # yt-dlp fetch seam → VideoArtifacts + SourceRef
│   ├── fuse.py             # Assemble transcript + on-screen text → VideoPayload
│   └── pipeline.py         # ingest_video: wires stages with per-stage artifact caching (ADR 0021); IngestionSeams/production_seams
│
└── scheduler/              # Autonomous trigger (ADR 0023): RSS new-episode detection + idempotent run-latest
    ├── __init__.py
    ├── channel.py          # YouTube RSS feed poll → newest video id (injected http_get; official feed, no scraping)
    ├── ledger.py           # Persistent processed-episodes ledger (user_state_folder), dedup by yt:<id>
    └── runner.py           # run_latest_once: detect → skip-if-processed → run pipeline if new → record on success

src/email_server/            # Deployable MCP server (separate process; single-sources SMTP defaults + EmailSendError from money_pit per ADR 0010)
├── __init__.py
└── server.py                # FastMCP email server: exposes send_email(to, subject, body)
```

---

# Grouping Rationale

**`schemas/` is built first.** Every other sub-package imports from it. Splitting by concern (rather than one `models.py`) makes `enums.py` the single authoritative source for all canonical enums — enum drift across files was the root cause of multiple boundary breaks identified in `pipeline_contracts.md`.

Each LLM boundary has two schema files: a `*_draft.py` (the model's raw output, input to the post-processor) and a contract module (what lands on disk and what downstream nodes read). `signal_draft.py` / `signals.py` and `analysis_draft.py` / `action_steps.py` apply this pattern. Mixing draft and contract models in one file obscures which types the LLM produces vs. which the deterministic layer produces.

**`graph/` contains zero business logic.** Keeping topology (node wiring, conditional edges) isolated means `graph.py` reads as a pure architecture diagram and each conditional edge in `edges.py` is trivially auditable as a deterministic function.

**`pipeline/` has one file per node.** Each node file owns read-of-upstream-artifact → call into `agents/` or `compute/` → write-of-output-artifact. `orchestration.py` handles schedule trigger, working-dir creation, and slug. `recovery.py` handles prior-journal reconciliation — a separate concern with a backward read dependency on execution output, resolved at day one rather than deferred.

**Shared node conventions.** `graph/state.py` exposes the accessors every node reads state through — `require_working_dir` / `require_slug` (fail loudly on a missing key) and `with_completed_step` (append to `completed_steps`) — so no node re-implements state extraction. Disk I/O is deliberately **not** centralized: each node owns its artifacts via private `_load_*_inputs` / `_write_*_outputs` helpers, keeping the read-then-compute-then-write shape uniform without a shared I/O layer that would blur which stage owns which file. (Modules + brief notes only; no ADR.)

**`agents/` is the visible LLM surface boundary.** If something is in `agents/`, it talks to a model. If it is in `compute/`, it provably does not. This makes the LLM footprint auditable as a directory listing. Note that `adapters/video_llm.py` (A1) is encapsulated inside the video adapter and is not a standalone pipeline stage; it is named `video_llm.py` (not `_llm.py`) so that "enumerate all LLM touch-points" searches find it without workarounds.

**`compute/tool_map.py` is the single home for both action-type maps.** The `ActionType → MCP tool name` and `ActionType → compensating tool` maps live here as plain dicts, imported by `pipeline/validator.py`, `compute/execution_params.py`, and `pipeline/execution.py`. This prevents the wrong dependency direction (pipeline nodes importing from other pipeline nodes) and eliminates the drift risk of three consumers maintaining their own copies.

**`schemas/macro.py` is kept separate from `schemas/answers.py`** so that `compute/regime.py` can import `MacroIndicators` without pulling in the full A3 answer contract dependency cone. The post-processor extracts `MacroIndicators` from `InitialAnswers` and passes it to `compute/regime.py`; the regime function itself has no reason to know about `Answer` or `InitialAnswers`.

**`compute/` is the highest-value test target.** Every function here is pure or near-pure. The architecture and pipeline contracts both enumerate specific computations that must not be in LLM (`pipeline_contracts.md Â§9`); `compute/` is the enforcement mechanism. `compute/aggregation.py` owns claim union, run-global re-ID, and tier max — deterministic functions that belong here, not inside the `pipeline/aggregator.py` node.

**`adapters/base.py` uses ABC, not Protocol.** Every adapter in this project is written in-house and Pydantic AI dependency injection may require concrete instantiation. An ABC with `abstract process(payload) -> SignalSet` makes the contract enforced at class definition time and is fully inspectable by basedpyright.

**`mcp/clients.py` exposes dependency types alongside factories.** Pydantic AI agents receive MCP clients via `RunContext` injection declared in `deps_type`. `AlpacaReadDeps` and `AlpacaWriteDeps` are the injection types; the factories construct them. The read/write safety boundary is structural: analysis agents are constructed with `AlpacaReadDeps`, the execution sub-agent with `AlpacaWriteDeps` — neither can reach the other's tool set by construction.

**`src/email_server/` is a separate top-level, separately-deployable package.** It runs as its own MCP-server process, but it is no longer import-isolated from `money_pit`: per ADR 0010 it single-sources the SMTP defaults (`DEFAULT_SMTP_HOST` / `DEFAULT_SMTP_PORT`) and the `EmailSendError` type from `money_pit`, killing the earlier drifting second copy of that config surface. Placing it at `src/email_server/` rather than `src/money_pit/servers/email/` keeps its process boundary and deployment story distinct.

---

# Design Tensions

1. **`pipeline/questions.py` and `agents/claim_questions.py`** — the node does most of A2 (templates, IDs, routing); the agent handles only claim-specific questions. The `claim_questions.py` name makes the scope explicit.

2. **`adapters/video_llm.py` is one of the four LLM cores but lives outside `agents/`** — correct given its encapsulation inside adapter execution, but worth noting in CLAUDE.md so "enumerate all LLM touch-points" searches check `adapters/` as well.

3. **`compute/execution_params.py` depends on the Alpaca MCP order tool's OpenAPI schema** (an external artifact). The snapshot lives at `mcp/alpaca_order_schema.json`, read through the shared `mcp/order_schema.py` loader by both `compute/execution_params.py` and `pipeline/validator.py`. The real `place_stock_order` schema has been pinned from the live Alpaca MCP server (`money-pit pin-order-schema`), so the sentinel is gone and the fail-closed gate is green; the emitted payload was reconciled to it — string `notional`/`qty` (ADR 0004, ADR 0007, ADR 0014).

---

# Verification

After scaffolding (creating empty `__init__.py` files and stub modules):

1. `python -c "from money_pit import schemas"` — confirms import graph works
2. `python -c "from money_pit.schemas.enums import ActionType"` — confirms enums are importable
3. `python -c "from email_server import server"` — confirms the email server is importable, including its ADR 0010 single-sourced imports of SMTP defaults + `EmailSendError` from `money_pit`
4. `basedpyright src/` — confirms no circular imports or missing stubs
5. `pytest tests/unit_tests/` — deterministic compute modules can be tested in isolation with no MCP or LLM dependencies

## Test directory correspondence

- `tests/unit_tests/compute/` — one test file per `compute/` module (highest-value target)
- `tests/integration_tests/pipeline/` — per-node integration tests with stubbed agents
- `tests/acceptance_tests/` — end-to-end paper-trading runs
