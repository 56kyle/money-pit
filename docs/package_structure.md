# Context

The `docs/architecture.md` and `docs/pipeline_contracts.md` docs are complete but no Python package structure exists beyond a handful of stub modules (`log.py`, `config.py`, `constants.py`, `trade.py`, `__main__.py`). The goal is to lay out the full `src/money_pit/` directory tree — module names, one-line responsibilities, grouping rationale — before any implementation begins, so the build order and import graph are intentional from day one.

---

# Proposed Package Structure

```
src/money_pit/
├── __init__.py              # (existing)
├── __main__.py              # (existing) CLI entry points via typer
├── log.py                   # (existing) loguru setup
├── config.py                # (existing) pydantic-settings Config + load_config
├── constants.py             # (existing) APP_NAME, paths, slug datetime format
│
├── schemas/                 # All Pydantic data contracts — single import source of truth
│   ├── __init__.py
│   ├── enums.py             # Every canonical enum: SignalTier, ClaimCategory, ActionType, ExecutionPhase, TerminalState, â€¦
│   ├── provenance.py        # SourceRef, SourceType
│   ├── signal_draft.py      # SignalSetDraft (raw A1 LLM output — before ticker norm, date parsing, validation)
│   ├── signals.py           # Claim, SignalSet, CorroborationEntry, AggregatedSignals (contracts)
│   ├── aggregation_draft.py # ClaimRelations (aggregator thin LLM output — agree/disagree labels)
│   ├── questions.py         # Question, InitialQuestions, SignalSummary
│   ├── question_draft.py    # DraftQuestion (A2 LLM output — claim-specific questions only)
│   ├── answers.py           # Answer, InitialAnswers
│   ├── answer_draft.py      # AnswerDraft (A3 LLM output — open-ended question answers)
│   ├── macro.py             # MacroIndicators (five-indicator snapshot consumed by compute/regime.py; isolated to keep regime's import surface narrow)
    ├── analysis_draft.py    # AnalysisJudgment (LLM draft from A4 — input to post-processor)
│   ├── action_steps.py      # ActionStep, ExecutionParameters, ActionSteps (post-processor output / A5 input)
│   ├── validation_results.py  # ValidationStep, ActionStepsValidation, ValidationStatus
│   ├── determination.py     # Determination
│   ├── journal.py           # ExecutionJournalEntry, ExecutionJournal
│   └── portfolio.py         # Position, PortfolioSnapshot
│
├── graph/                   # LangGraph wiring only — zero business logic
│   ├── __init__.py
│   ├── state.py             # TypedDict for graph state: slug, working_dir, completed_steps, terminal_state
│   ├── graph.py             # StateGraph assembly: add_node / add_edge / add_conditional_edges
│   └── edges.py             # Conditional edge functions: signal_gate, terminal_state_router, determination_gate
│
├── pipeline/                # One module per LangGraph node; owns file I/O for its stage
│   ├── __init__.py
│   ├── orchestration.py     # Scheduler trigger, working-dir creation, slug assignment
│   ├── recovery.py          # Prior-journal recovery: reads execution_journal.json, reconciles before planning
│   ├── snapshot.py          # Calls Alpaca read MCP, writes portfolio_snapshot.json
│   ├── aggregator.py        # Merges SignalSets, re-IDs claims, computes run-level has_actionable_content
│   ├── questions.py         # A2 node: template emission, ID assignment, routing-table data_sources, file writes
│   ├── retrieval.py         # A3 node: deterministic known-param fetch, budget control, file writes
│   ├── analysis.py          # A4 node: calls agents/thesis_judgment + compute/ post-processor functions, writes action_steps.json
│   ├── validator.py         # A5: manifest parse, jsonschema checks, three-file write; consumes compute/tool_map.py for action_type lookup
│   ├── execution.py         # Execution sub-agent: transactional loop, idempotent orders, journal, compensation
│   └── notification.py      # Notification sub-agent: email templating per terminal state, send_email call
│
├── agents/                  # Thin LLM cores (Pydantic AI); no deterministic logic, no file I/O
│   ├── __init__.py
│   ├── corroboration.py     # Aggregator's thin LLM pass: label agree/disagree on pre-clustered claim groups
│   ├── claim_questions.py   # A2 LLM core: claim-specific thesis-validation + invalidation questions only
│   ├── answer_synthesis.py  # A3 LLM core: open-ended Brave/EDGAR lookups + answer synthesis
│   └── thesis_judgment.py   # A4 LLM core: claim disposition, thesis narratives, scenarios → AnalysisJudgment
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
│   └── execution_params.py  # ActionType + judgment → execution_parameters with literal Alpaca MCP field names
│
└── mcp/                     # MCP client configuration; runs inside the pipeline process
    ├── __init__.py
    ├── clients.py           # AlpacaReadDeps, AlpacaWriteDeps, ResearchDeps dep types + factory fns; injection-ready for Pydantic AI
    ├── manifest.py          # Introspects registered MCP servers at runtime
    └── alpaca_order_schema.json  # Pinned Alpaca MCP order tool inputSchema snapshot; single source of truth for compute/execution_params.py and pipeline/validator.py → tool manifest consumed by A5

src/email_server/            # Deployable MCP server (separate process; no imports from money_pit package)
├── __init__.py
└── server.py                # FastMCP email server: exposes send_email(to, subject, body)
```

---

# Grouping Rationale

**`schemas/` is built first.** Every other sub-package imports from it. Splitting by concern (rather than one `models.py`) makes `enums.py` the single authoritative source for all canonical enums — enum drift across files was the root cause of multiple boundary breaks identified in `pipeline_contracts.md`.

Each LLM boundary has two schema files: a `*_draft.py` (the model's raw output, input to the post-processor) and a contract module (what lands on disk and what downstream nodes read). `signal_draft.py` / `signals.py` and `analysis_draft.py` / `action_steps.py` apply this pattern. Mixing draft and contract models in one file obscures which types the LLM produces vs. which the deterministic layer produces.

**`graph/` contains zero business logic.** Keeping topology (node wiring, conditional edges) isolated means `graph.py` reads as a pure architecture diagram and each conditional edge in `edges.py` is trivially auditable as a deterministic function.

**`pipeline/` has one file per node.** Each node file owns read-of-upstream-artifact → call into `agents/` or `compute/` → write-of-output-artifact. `orchestration.py` handles schedule trigger, working-dir creation, and slug. `recovery.py` handles prior-journal reconciliation — a separate concern with a backward read dependency on execution output, resolved at day one rather than deferred.

**`agents/` is the visible LLM surface boundary.** If something is in `agents/`, it talks to a model. If it is in `compute/`, it provably does not. This makes the LLM footprint auditable as a directory listing. Note that `adapters/video_llm.py` (A1) is encapsulated inside the video adapter and is not a standalone pipeline stage; it is named `video_llm.py` (not `_llm.py`) so that "enumerate all LLM touch-points" searches find it without workarounds.

**`compute/tool_map.py` is the single home for both action-type maps.** The `ActionType → MCP tool name` and `ActionType → compensating tool` maps live here as plain dicts, imported by `pipeline/validator.py`, `compute/execution_params.py`, and `pipeline/execution.py`. This prevents the wrong dependency direction (pipeline nodes importing from other pipeline nodes) and eliminates the drift risk of three consumers maintaining their own copies.

**`schemas/macro.py` is kept separate from `schemas/answers.py`** so that `compute/regime.py` can import `MacroIndicators` without pulling in the full A3 answer contract dependency cone. The post-processor extracts `MacroIndicators` from `InitialAnswers` and passes it to `compute/regime.py`; the regime function itself has no reason to know about `Answer` or `InitialAnswers`.

**`compute/` is the highest-value test target.** Every function here is pure or near-pure. The architecture and pipeline contracts both enumerate specific computations that must not be in LLM (`pipeline_contracts.md Â§9`); `compute/` is the enforcement mechanism. `compute/aggregation.py` owns claim union, run-global re-ID, and tier max — deterministic functions that belong here, not inside the `pipeline/aggregator.py` node.

**`adapters/base.py` uses ABC, not Protocol.** Every adapter in this project is written in-house and Pydantic AI dependency injection may require concrete instantiation. An ABC with `abstract process(payload) -> SignalSet` makes the contract enforced at class definition time and is fully inspectable by basedpyright.

**`mcp/clients.py` exposes dependency types alongside factories.** Pydantic AI agents receive MCP clients via `RunContext` injection declared in `deps_type`. `AlpacaReadDeps` and `AlpacaWriteDeps` are the injection types; the factories construct them. The read/write safety boundary is structural: analysis agents are constructed with `AlpacaReadDeps`, the execution sub-agent with `AlpacaWriteDeps` — neither can reach the other's tool set by construction.

**`src/email_server/` is a separate top-level package.** It has no import relationship to `money_pit`. Placing it at `src/email_server/` rather than `src/money_pit/servers/email/` means `import money_pit.servers.email.server` is impossible, which matches the stated isolation invariant.

---

# Design Tensions

1. **`pipeline/questions.py` and `agents/claim_questions.py`** — the node does most of A2 (templates, IDs, routing); the agent handles only claim-specific questions. The `claim_questions.py` name makes the scope explicit.

2. **`adapters/video_llm.py` is one of the four LLM cores but lives outside `agents/`** — correct given its encapsulation inside adapter execution, but worth noting in CLAUDE.md so "enumerate all LLM touch-points" searches check `adapters/` as well.

3. **`compute/execution_params.py` depends on the Alpaca MCP order tool's OpenAPI schema** (an external artifact). The pinned snapshot lives at `mcp/alpaca_order_schema.json` and is read by both `compute/execution_params.py` and `pipeline/validator.py`; both are blocked until this file is populated from the live Alpaca MCP server at integration time.

---

# Verification

After scaffolding (creating empty `__init__.py` files and stub modules):

1. `python -c "from money_pit import schemas"` — confirms import graph works
2. `python -c "from money_pit.schemas.enums import ActionType"` — confirms enums are importable
3. `python -c "from email_server import server"` — confirms email server is importable with no cross-package contamination
4. `basedpyright src/` — confirms no circular imports or missing stubs
5. `pytest tests/unit_tests/` — deterministic compute modules can be tested in isolation with no MCP or LLM dependencies

## Test directory correspondence

- `tests/unit_tests/compute/` — one test file per `compute/` module (highest-value target)
- `tests/integration_tests/pipeline/` — per-node integration tests with stubbed agents
- `tests/acceptance_tests/` — end-to-end paper-trading runs




