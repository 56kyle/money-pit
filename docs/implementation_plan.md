# money-pit: Python Implementation Plan

## Context

Architecture phase is complete. All six agent prompts, `docs/design_decisions.md` (§1–§8), `docs/package_structure.md`, `docs/pipeline_contracts.md`, and `docs/architecture.md` are finalized and consistent. The goal is to implement the deterministic spine — the part that moves money — and get it fully green on paper trading with zero LLM calls before any real agent is written.

**Key constraint:** "If the spine is solid, an agent producing slightly-off output degrades gracefully; if the spine is shaky, a perfect agent still loses money."

**Source documents:** `docs/package_structure.md` (canonical module list), `docs/design_decisions.md` (regime + sizing specs), `docs/pipeline_contracts.md` (boundary schemas).

---

## Current State

**Phase 1 (`schemas/`) — COMPLETE.**
- All 17 schema files implemented (Pydantic v2, `frozen=True`, `extra='forbid'`, `ClassVar[ConfigDict]`)
- `schemas/macro.py` created; `schemas/__init__.py` re-exports all 49 public types
- `Config` extended with 10 regime/sizing knobs + 5 indicator threshold fields (env var overrides, sensible defaults)
- `tests/unit_tests/schemas/test_enum_coverage.py` — 16/16 passing
- `basedpyright src/money_pit/schemas/ src/money_pit/config.py` — 0 errors, 0 warnings
- `tests/conftest.py` updated: `pytest-repo-structure` plugin is optional

**Phase 2 (`compute/`) — COMPLETE.**
- All 9 compute modules implemented; `compute/tool_map.py` created as a new file
- 96 unit tests passing; basedpyright 0 errors, 0 warnings
- `docs/decisions/0001-regime-scalar-threshold-v0.md` (MADR v4) written alongside `regime.py`
- Shared test fixtures extracted to `tests/unit_tests/compute/conftest.py`

**Phase 3 (`graph/edges.py`) — COMPLETE.**
- `PipelineState` TypedDict (`graph/state.py`), three gate functions (`graph/edges.py`) implemented
- 13 unit tests passing; basedpyright 0 errors, 0 warnings
- Routing string constants (`PROCEED`, `NO_ACTION`, `VALIDATE`, `HALT`, `EXECUTE`, `NOTIFY`) defined in `edges.py`

**Phase 4 (deterministic spine) — COMPLETE.**
- 10 pipeline nodes implemented: `snapshot`, `aggregator`, `questions`, `retrieval`, `analysis`, `validator`, `execution`, `notification` + inline `no_action_terminal`
- `graph/graph.py` assembles `StateGraph[PipelineState]` with all conditional edges
- `pipeline/orchestration.py` wires Phase 4 stubs and exposes `run_pipeline()`
- `mcp/alpaca_order_schema.json` stubbed (real schema pinned in Phase 7)
- `langgraph>=0.2.0` and `jsonschema>=4.0.0` added to dependencies
- Integration tests: 2/2 passing (execute path + no_action path); all working-dir files schema-valid; zero LLM calls; zero real Alpaca orders
- basedpyright: 0 errors across all pipeline modules (LangGraph stub warnings are structural noise)

**Phase 5 (LLM cores) — COMPLETE.**
- Pydantic AI 2.1.0 agents implemented for A2 (claim_questions), A3 (answer_synthesis), A4 (thesis_judgment)
- System prompts loaded verbatim from `data/agents/agent_N.md` at module import time
- `DeterministicResearchTools` and `OpenEndedResearchTools` Protocols defined in `agents/research_tools.py`
- `retrieval.py` split: macro_regime/portfolio_gap → FRED/yfinance SDK; open-ended questions → A3 LLM
- `pipeline/orchestration.py` wired with Phase 5 agents; `PipelineOverrides` dataclass for test injection
- Agent-failure contracts applied: A4→ANALYSIS_HALT (hard), A2/A3→empty list (soft), A5→UNMATCHED (conservative)
- 112/112 tests passing; 0 basedpyright errors

**Phase 6 (Source Adapters) — COMPLETE.**
- `adapters/base.py`: `SourceAdapter[T]` generic ABC; ADR in `docs/decisions/0002-source-adapter-pattern.md`
- `adapters/video_llm.py`: `VideoPayload` Pydantic model + `TranscriptSource` enum + `make_video_llm_agent` A1 factory loading `data/agents/agent_1.md`
- `adapters/video.py`: `VideoAdapter(SourceAdapter[VideoPayload])` — pre-LLM persistence, `SignalSetDraft → SignalSet` post-processing
- 6 integration tests passing; 118/118 total; 0 basedpyright errors
- Step 2 (yt-dlp + caption-first cascade + WhisperX forced alignment) deferred to Phase 7 (stepping-stone terminus marked in `VideoPayload`)

**Phase 7 (MCP Layer and Email Server) — next.**

---

## Phase 1: `schemas/` — COMPLETE

All 17 schema files implemented in dependency order. Key constraints:
- `(str, Enum)` base class throughout (not `StrEnum`) — Python 3.10 compat + basedpyright clean
- `model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")` on every model
- `schemas/macro.py` (`MacroIndicators`) isolated so `compute/regime.py` doesn't import answer contracts
- `schemas/analysis_draft.py`: `claim_id`-keyed (no `step_id`); `step_id` first minted by post-processor
- `schemas/action_steps.py`: has `step_id`, `regime_tag`, `dollar_amount`, `execution_parameters`

---

## Phase 2: `compute/` — COMPLETE

Pure functions, no I/O, no LLM. Key design decisions resolved:

### Resolved: `regime.py` scalar threshold approach

`design_decisions.md §1` described `discretize(series: list[float], ...)` (z-scores from trailing window). `MacroIndicators` holds a single scalar per indicator (what FRED retrieval returns). These are incompatible.

**v0 resolution:** `discretize(value, threshold, orientation, band) -> int | None`. Five structural thresholds:

| indicator | threshold | orientation |
|---|---|---|
| `yield_curve` (T10Y2Y) | 0.0 | +1 |
| `credit_spreads` (HY OAS pct pts) | 3.0 | -1 |
| `pmi` (ISM Mfg) | 50.0 | +1 |
| `earnings_revisions` (breadth fraction) | 0.0 | +1 |
| `inflation` (CPILFESL YoY %) | 2.5 | -1 |

`regime_lookback` unused in v0. `RECOVERY` rule dropped (needs prior-period state). ADR: `docs/decisions/0001-regime-scalar-threshold-v0.md`.

### Regime truth table (from `design_decisions.md §1b`, exact)

`growth = pmi + earnings`. Ordered, first match wins:

| Rule | Tag | Condition |
|---|---|---|
| 0 | UNCERTAIN | any indicator None |
| 1 | LATE_CYCLE_STRESS | curve==-1 AND credit==-1 AND pmi<=0 AND earnings<=0 |
| 2 | STAGFLATION | credit<=0 AND pmi<=0 AND earnings<=0 AND inflation==-1 |
| 3 | GROWTH_ACCELERATING | curve>=0 AND credit>=0 AND pmi==1 AND earnings==1 AND inflation>=0 |
| 4 | RECOVERY | DROPPED in v0 |
| 5 | GROWTH_DECELERATING | credit>=0 AND pmi<=0 AND earnings<=0 AND inflation>=0 |
| guard | UNCERTAIN | (curve+credit)>=1 while growth<=-1, or (curve+credit)<=-1 while growth>=1 |
| fallback | varies | total=curve+credit+pmi+earnings: >1→GROWTH_ACCELERATING; <-1→GROWTH_DECELERATING; else UNCERTAIN |

### Sizing (`sizing.py`)

`solve_kelly`: bisection on `g(f) = Σ p_i·r_i / (1 + f·r_i) = 0`. Upper bound capped at `_MAX_KELLY_FRACTION = 1.0`; when analytical optimum exceeds 1.0, bisection saturates there and `max_position_weight` does the pull-back.

`size_position`: EV gate → Kelly → haircuts → weight cap → dollar conversion → headroom clamps → None if ≤ 0.

### Tool map (`tool_map.py`)

`ACTION_TYPE_TO_TOOL` and `COMPENSATING_ACTION`. All Alpaca order operations use `place_order`; primary vs. compensating is distinguished by `side` (buy/sell), not by tool name. Callers needing the compensating tool use `ACTION_TYPE_TO_TOOL[COMPENSATING_ACTION[action_type]]`.

### Confidence (`confidence.py`)

`_PRIMARY = {fred_mcp, edgartools_mcp, alpaca_mcp}`, `_SECONDARY = {yfinance_mcp}`, `_TERTIARY = {brave_search_mcp}`. Rule: any Brave → LOW; any non-primary source → MEDIUM; all primary → HIGH.

### Execution params (`execution_params.py`)

Intentionally CI-red until `mcp/alpaca_order_schema.json` is pinned from live Alpaca MCP server. `_load_alpaca_schema()` raises `FileNotFoundError` with a clear message if absent.

---

## Phase 3: `graph/edges.py` — Three Gates with Hand-Built JSON Tests

All three live in `graph/edges.py` as pure functions of `PipelineState`. **Test all three with hand-built JSON before any pipeline node is written.**

```python
def signal_gate(state: PipelineState) -> str:       # → "proceed" | "no_action"
def terminal_state_router(state: PipelineState) -> str:  # → "validate" | "halt"
def determination_gate(state: PipelineState) -> str: # → "execute" | "notify"
```

`PipelineState` is a `TypedDict` defined in `graph/state.py` (Phase 4). For Phase 3 tests, define a minimal inline dict or stub.

**Tests** (`tests/unit_tests/graph/test_edges.py`):
- `signal_gate`: `has_actionable_content=True` → "proceed"; `False` → "no_action"
- `terminal_state_router`: `terminal_state` set → "halt"; not set → "validate"
- `determination_gate`: all steps `MATCHED` → "execute"; any `UNMATCHED` → "notify"; empty steps → "notify"
- Each gate tested with hand-authored minimal dicts, not real pipeline artifacts

Create `tests/unit_tests/graph/__init__.py` before writing tests.

---

## Phase 4: Deterministic Spine with Stubbed Agents — COMPLETE

**Goal:** Run the entire pipeline on hand-authored JSON with zero LLM calls, on paper trading, with no real Alpaca orders.

Build order:

1. **`graph/state.py`** — `PipelineState` TypedDict: `slug`, `working_dir`, `completed_steps`, `terminal_state`
2. **`pipeline/orchestration.py`** — slug creation (`YYYY-MM-DD_HH-MM-SS`), working-dir creation, graph entry
3. **`pipeline/snapshot.py`** — calls Alpaca read MCP, writes `portfolio_snapshot.json`
4. **`pipeline/aggregator.py`** — reads `signals/{source_id}.json`, runs `compute/aggregation.py`, writes `aggregated_signals.json`; at N=1: `corroborations: []`, `conflicts: []`
5. **`pipeline/questions.py`** — A2 node; uses `compute/routing.py`; writes `initial_questions.json` + `.md`; LLM core stubbed
6. **`pipeline/retrieval.py`** — A3 node; stubbed to return hand-authored `initial_answers.json`
7. **`pipeline/analysis.py`** — A4 + post-processor:
   - A4 stubbed (returns hand-authored `AnalysisJudgment`)
   - Post-processor is real: `compute/regime.py` → constraint extraction → `compute/sizing.py` EV gate → Kelly → `compute/execution_params.py`
   - Mints `step_id` (`A001`, `A002`, ...) deterministically
   - Writes `analysis_judgment.json` and `action_steps.json`
8. **`pipeline/validator.py`** — A5 node; reads MCP manifest; `jsonschema.validate` for checks 1, 2, 4; LLM for check 3 (behavioral match); writes `action_steps_validation.json`, `.md`, `validation_status.json`
9. **`pipeline/execution.py`** / **`pipeline/notification.py`** — deterministic wrappers; paper-trading via Alpaca sandbox
10. **`graph/graph.py`** — `StateGraph` wiring all nodes and conditional edges

**Acceptance criterion:** paper-trade run completes end-to-end; every working-dir file present and schema-valid; zero LLM calls; zero real Alpaca orders.

---

## Phase 5: LLM Cores (`agents/`)

Write only after the spine is green.

1. **`agents/thesis_judgment.py`** — A4 LLM core (Pydantic AI): `aggregated_signals.json` + `portfolio_snapshot.json` + `initial_answers.json` → `AnalysisJudgment` list
2. **`agents/claim_questions.py`** — A2 LLM core: claim-specific thesis-validation + invalidation questions (portfolio/macro questions are deterministic)
3. **`agents/answer_synthesis.py`** — A3 LLM core: open-ended retrieval + answer synthesis
4. **`agents/corroboration.py`** — N>1 stub; returns `ClaimRelations(agree=[], disagree=[])` until N>1 source path is wired

---

## Phase 6: Source Adapters (`adapters/`)

1. **`adapters/base.py`** — ABC with `abstract process(payload) -> SignalSet`
2. **`adapters/video_llm.py`** — A1 LLM core: transcript + `SourceRef` → `SignalSetDraft`
3. **`adapters/video.py`** — full adapter: yt-dlp + WhisperX + PySceneDetect + OCR → `video_llm.py`; deterministic fetch output persisted before LLM classification so expensive ingestion is cached separately

---

## Phase 7: MCP Layer and Email Server

1. **`mcp/clients.py`** — `AlpacaReadDeps`, `AlpacaWriteDeps`, `ResearchDeps` dep types + factory functions
2. **`mcp/manifest.py`** — runtime introspection of registered MCP servers → tool manifest for A5
3. **`mcp/alpaca_order_schema.json`** — **discrete checkpoint**: pin from live Alpaca MCP server; `compute/execution_params.py` and `pipeline/validator.py` both blocked until this file exists
4. **`src/email_server/server.py`** — FastMCP email server: `send_email(to, subject, body)`; no import relationship to `money_pit` package

---

## Phase 8: `pipeline/recovery.py`

Prior-journal reconciliation: reads `execution_journal.json`, checks for in-flight orders (partial fills, compensation needed), reconciles before planning a new run.

---

## Key Checkpoints

| Checkpoint | Criterion |
|---|---|
| schemas/ complete ✓ | `python -c "from money_pit import schemas"` clean; basedpyright clean; enum guard passes |
| compute/ complete ✓ | All property tests green; every reachable regime tag covered; 96 tests passing |
| gates complete ✓ | Three `graph/edges.py` gate tests pass on hand-built JSON |
| spine green | End-to-end paper-trade run with stub agents; all working-dir files schema-valid; zero LLM calls |
| schema pinned | `mcp/alpaca_order_schema.json` populated from live Alpaca MCP; `execution_params` validates against it |
| full pipeline | End-to-end with real agents on paper-trading account; `determination.json` written; email or execution triggered correctly |

---

## Watch Items

- **`mcp/alpaca_order_schema.json`**: cannot be synthesized; must be pinned from the live Alpaca MCP server. Treat as a deployment gate, not a code-complete milestone.
- **N=1 stub inertness** (`test_n1_stubs.py`): turns red when a second source or the first interdependent thesis activates a dormant aggregation path.
- **`terminal_state_router`**: the gate routing to "validate" vs "halt" depends on which `TerminalState` values are considered halting — confirm against `pipeline_contracts.md` before implementing.

---

## Test Directory Layout

```
tests/
├── unit_tests/
│   ├── schemas/
│   │   └── test_enum_coverage.py        # enum membership guard
│   ├── compute/
│   │   ├── conftest.py                  # shared SourceRef + make_claim fixtures
│   │   ├── test_signal_flags.py
│   │   ├── test_aggregation.py
│   │   ├── test_factor_profile.py
│   │   ├── test_confidence.py
│   │   ├── test_regime.py               # property tests
│   │   ├── test_sizing.py               # property tests
│   │   ├── test_n1_stubs.py             # stub inertness
│   │   └── test_execution_params.py     # CI-red until schema pinned
│   └── graph/
│       └── test_edges.py                # three gates, hand-built JSON
├── integration_tests/
│   └── pipeline/
└── acceptance_tests/
    └── paper_trade/
```
