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

**Post-Phase-6 conformance review + remediation — COMPLETE.**

- A conformance review (`docs/reviews/phase-1-6-findings.md`) audited the shipped spine against the
  pinned docs; remediation ran as waves P1/P2/S1–S8, followed by a behavior-preserving **node refactor**
  (shared `graph/state.py` accessors + per-node `_load_*`/`_write_*` helpers).
- Design calls made during remediation are recorded in accepted **ADRs 0003–0006**: execution-journal
  semantics (nullable `outcome`, submission-level `EXECUTED_CLEAN`, `AtomicGroupNotSupportedError`);
  static pinned A5 manifest; A4 Step-1 disposition + macro-read-as-narrative; determination node +
  finalizer with the 4-member `TerminalState`. The narrative docs (`architecture.md`,
  `pipeline_contracts.md`, `package_structure.md`) now describe the as-built design and cross-link these ADRs.

**Phase 7 (MCP Layer and Email Server) — COMPLETE; order schema pinned from the live server (ADR 0014).**

- Integration-time reality corrected the spec (ADR 0008): the real `alpaca-mcp-server` has **no
  `place_order`** (equities use `place_stock_order`), toolset names differ (`account`/`trading`/
  `stock-data`, not `market-data`), and live positions share the `trading` toolset with the order
  tools. `compute/tool_map.py` + the manifest retargeted to `place_stock_order`.
- **Read = alpaca-py, write = MCP** (ADR 0008): `alpaca_portfolio.py` fetches the snapshot via the
  alpaca-py `TradingClient` (so no positions-read client holds an order tool); `mcp/clients.py`
  (`AlpacaWriteDeps`) is a **connect-per-call** `OrderPlacer` over a `trading`-scoped stdio
  `alpaca-mcp-server`. `AlpacaReadDeps`/fused `ResearchDeps` **dropped** — no consumer (the yfinance
  shim already backs `fetch_ticker_price`); the only research gap, `edgar_search`, is now implemented
  via `edgartools` in `_DirectOpenEndedTools`.
- `mcp/manifest.py` — `live_manifest(credentials)` added (introspects the connected server; fails
  closed via `ManifestUnavailableError`); `pinned_manifest()` remains the static default (ADR 0004).
- `money-pit pin-order-schema` CLI introspects `place_stock_order`'s `inputSchema` and writes the
  pinned `alpaca_order_schema.json` (sentinel stripped) — the repeatable ADR-0007 deployment pin.
- Credentials: typed `AlpacaCredentials` + `resolve_alpaca_credentials` (keyring; paper/live from a
  `-paper`/`-live` service suffix; `CredentialResolutionError` fail-closed).
- Email (ADR 0009): real `email_sender.py` (smtplib/Gmail, typed `EmailSendError`) wired as the
  `EmailSender`; independent FastMCP `email_server/server.py` (`send_email`, no `money_pit` import).
- Composition: `production_deps(config)` builds the real capital seams; it is **never auto-invoked** —
  a bare `run_pipeline()` still fails closed (`_require_capital_critical_deps`).
- Tests: offline unit suite (mock-free; new `CredentialResolutionError`/`ManifestUnavailableError`/
  `EmailSendError` fail-closed `pytest.raises`); opt-in `@pytest.mark.live` paper tier
  (`tests/acceptance_tests/paper_trade/`, deselected by default). Every Phase-7 source file at 100%.
- `mcp/alpaca_order_schema.json` — **now pinned** from the live server (`pin-order-schema` run; stub
  sentinel removed), flipping the ADR-0007 gate green; the payload was reconciled to the real schema
  (string `notional`/`qty`, cents-formatted notional — ADR 0014) and guarded against invalid amounts (ADR 0015).

**Fill observability (independent-order path) — COMPLETE (ADR 0017).**

- The execution node now **observes** each submitted order's real fill via a new `FillObserver` dep
  (alpaca-py `get_order_by_client_id`, reads-via-SDK per ADR 0008), journaling the true phase
  (`FILLED`/`PARTIALLY_FILLED`/`REJECTED`/`SUBMITTED`) and fill fields; the poll loop fails closed
  (404/transient = retryable in-flight, never a fabricated fill).
- `EXECUTED_CLEAN` reverts to "all legs **filled**"; new `EXECUTED_INCOMPLETE` = "submitted but not
  cleanly filled" → routes to `failure` **and** emails the owner ("Execution Incomplete"). New `Config`
  knobs `execution_fill_poll_interval_seconds` (1.0) / `execution_fill_poll_timeout_seconds` (30.0).
  New modules `alpaca_orders.py`, `schemas/fills.py`, `compute/fills.py`. Atomic-group compensation
  remains the deferred `AtomicGroupNotSupportedError` stub.
- This **unblocks Phase 8 recovery**: its input — real observed fills in the journal — now exists.

**Phase 8 (recovery) — COMPLETE (ADR 0018).**

- Recovery reconciles the prior run's open orders as the **graph entry node** (runs before
  snapshot/planning). It re-observes the most recent prior run's potentially-open legs via the fill
  observer and **halts** the new run on any still-open order (double-exposure → email "Recovery Halt",
  `recovery_router` routes → END, nothing planned), emits a **notice** and proceeds on an
  abnormal-but-settled prior run (`EXECUTED_INCOMPLETE` / crashed `outcome=None` → email "Prior Run
  Reconciled"), else proceeds silently; a `recovery.json` audit artifact is written every run.
- Builds on the ADR 0017 observed fills; the fresh snapshot handles every filled leg, so recovery only
  closes the open-order gap. **No auto-unwind**, and the `COMPENSATION_FAILED` reconciliation branch stays
  deferred with the atomic-group work.
- New: `schemas/recovery.py` (`PriorRunReconciliation`, `ReconciledOrder`), `RecoveryDecision` enum,
  `reconcile_prior_run` + `make_recovery_node`, `recovery_router` + `HALT` literal, `recovery_decision`
  state key, `RECOVERY_JSON_FILENAME` constant. Resolves architecture §15 #12.

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

| indicator                               | threshold | orientation |
| --------------------------------------- | --------- | ----------- |
| `yield_curve` (T10Y2Y)                  | 0.0       | +1          |
| `credit_spreads` (HY OAS pct pts)       | 3.0       | -1          |
| `pmi` (ISM Mfg)                         | 50.0      | +1          |
| `earnings_revisions` (breadth fraction) | 0.0       | +1          |
| `inflation` (CPILFESL YoY %)            | 2.5       | -1          |

`regime_lookback` unused in v0. `RECOVERY` rule dropped (needs prior-period state). ADR: `docs/decisions/0001-regime-scalar-threshold-v0.md`.

### Regime truth table (from `design_decisions.md §1b`, exact)

`growth = pmi + earnings`. Ordered, first match wins:

| Rule     | Tag                 | Condition                                                                                        |
| -------- | ------------------- | ------------------------------------------------------------------------------------------------ |
| 0        | UNCERTAIN           | any indicator None                                                                               |
| 1        | LATE_CYCLE_STRESS   | curve==-1 AND credit==-1 AND pmi<=0 AND earnings<=0                                              |
| 2        | STAGFLATION         | credit<=0 AND pmi<=0 AND earnings<=0 AND inflation==-1                                           |
| 3        | GROWTH_ACCELERATING | curve>=0 AND credit>=0 AND pmi==1 AND earnings==1 AND inflation>=0                               |
| 4        | RECOVERY            | DROPPED in v0                                                                                    |
| 5        | GROWTH_DECELERATING | credit>=0 AND pmi<=0 AND earnings<=0 AND inflation>=0                                            |
| guard    | UNCERTAIN           | (curve+credit)>=1 while growth<=-1, or (curve+credit)<=-1 while growth>=1                        |
| fallback | varies              | total=curve+credit+pmi+earnings: >1→GROWTH_ACCELERATING; <-1→GROWTH_DECELERATING; else UNCERTAIN |

### Sizing (`sizing.py`)

`solve_kelly`: bisection on `g(f) = Σ p_i·r_i / (1 + f·r_i) = 0`. Upper bound capped at `_MAX_KELLY_FRACTION = 1.0`; when analytical optimum exceeds 1.0, bisection saturates there and `max_position_weight` does the pull-back.

`size_position`: EV gate → Kelly → haircuts → weight cap → dollar conversion → headroom clamps → None if ≤ 0.

### Tool map (`tool_map.py`)

`ACTION_TYPE_TO_TOOL` and `COMPENSATING_ACTION`. All Alpaca order operations use `place_stock_order` (ADR 0008 — there is no `place_order` tool); primary vs. compensating is distinguished by `side` (buy/sell), not by tool name. Callers needing the compensating tool use `ACTION_TYPE_TO_TOOL[COMPENSATING_ACTION[action_type]]`.

### Confidence (`confidence.py`)

`_PRIMARY = {fred_mcp, edgartools_mcp, alpaca_mcp}`, `_SECONDARY = {yfinance_mcp}`, `_TERTIARY = {brave_search_mcp}`. Rule: any Brave → LOW; any non-primary source → MEDIUM; all primary → HIGH.

### Execution params (`execution_params.py`)

Loading is via the shared `mcp/order_schema.load_order_schema` (`compute/execution_params.py`
and `pipeline/validator.py` share the one `ALPACA_ORDER_SCHEMA_PATH`), raising the typed
`AlpacaOrderSchemaMissingError` (not a bare `FileNotFoundError`) when absent, `AlpacaOrderSchemaMalformedError`
when unparseable, and `AlpacaOrderSchemaNotPinnedError` while a stub sentinel is present. The real
`place_stock_order` schema has since been pinned from the live server (sentinel removed), so the gate is
green; the emitted payload was reconciled to it — string `notional`/`qty`, cents-formatted notional, plus
an `InvalidExecutionAmountError` guard rejecting non-finite/sub-cent amounts before schema load (ADRs 0014, 0015).

---

## Phase 3: `graph/edges.py` — Three Gates with Hand-Built JSON Tests

All three live in `graph/edges.py` as pure functions of `PipelineState`. **Test all three with hand-built JSON before any pipeline node is written.**

```python
def signal_gate(state: PipelineState) -> str:            # → "proceed" | "no_action"
def terminal_state_router(state: PipelineState) -> str:  # → "validate" | "no_action" | "notify"
def determination_router(state: PipelineState) -> str:   # → "execute" | "notify" | "finalize"
def post_notification_router(state: PipelineState) -> str:  # → "terminate" | "finalize"
```

(`determination_router` is the 3-branch as-built successor to the earlier 2-branch `determination_gate`;
`post_notification_router` was added so the validation-error notification rejoins the finalizer — ADR 0006.)

`PipelineState` is a `TypedDict` defined in `graph/state.py` (Phase 4). For Phase 3 tests, define a minimal inline dict or stub.

**Tests** (`tests/unit_tests/graph/test_edges.py`):

- `signal_gate`: `has_actionable_content=True` → "proceed"; `False` → "no_action"
- `terminal_state_router`: `terminal_state is None` → "validate"; `NO_ACTION` → "no_action"; else → "notify"
- `determination_router`: `PROCEED` (terminal_state None) → "execute"; `VALIDATION_ERROR` → "notify"; `ORCHESTRATION_ERROR` → "finalize"
- `post_notification_router`: `VALIDATION_ERROR` → "finalize"; else (`ANALYSIS_HALT`) → "terminate"
- Each gate tested with hand-authored minimal dicts, not real pipeline artifacts

Create `tests/unit_tests/graph/__init__.py` before writing tests.

---

## Phase 4: Deterministic Spine with Stubbed Agents — COMPLETE

**Goal:** Run the entire pipeline on hand-authored JSON with zero LLM calls, on paper trading, with no real Alpaca orders.

Build order:

1. **`graph/state.py`** — `PipelineState` TypedDict (10 control keys): `slug`, `working_dir`, `completed_steps`, `terminal_state`, `run_has_actionable_content`, `validation_steps`, `determination`, `failed_steps`, `sub_agent_spawned`, `determination_reason` — plus the `require_working_dir`/`require_slug`/`with_completed_step` accessors
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
8. **`pipeline/validator.py`** — A5 node; reads the static pinned manifest (`pinned_manifest()` over `mcp/order_schema.py`, ADR 0004); `jsonschema.validate` for schema acceptance; check 3 is the deterministic `ACTION_TYPE_TO_TOOL` lookup (the injected LLM `behavioral_match` predicate was **removed**); writes `action_steps_validation.json`, `.md`, `validation_status.json`
9. **`pipeline/determination.py`** — A6: `recompute_determination` decision node + finalizer that writes `determination.json/.md` once (ADR 0006)
10. **`pipeline/execution.py`** / **`pipeline/notification.py`** — deterministic wrappers; paper-trading via Alpaca sandbox
11. **`graph/graph.py`** — `StateGraph` wiring all nodes and conditional edges

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

## Phase 7: MCP Layer and Email Server — COMPLETE; schema pinned

**As-built (ADR 0008, ADR 0009); see the Current-State block above for the full summary.** The spec's
`place_order` / `market-data` assumptions were corrected to the real `alpaca-mcp-server` taxonomy at
integration time.

1. **`mcp/clients.py`** — `AlpacaWriteDeps` + `make_alpaca_write_deps`: connect-per-call `OrderPlacer`
   over a `trading`-scoped stdio `alpaca-mcp-server`, placing `place_stock_order`. `AlpacaReadDeps`
   and the fused `ResearchDeps` were **dropped** (no consumer). Reads moved to alpaca-py
   (`alpaca_portfolio.py`); `edgar_search` implemented via `edgartools` in `_DirectOpenEndedTools`.
2. **`mcp/manifest.py`** — `live_manifest(credentials)` introspects the connected server (fails closed
   via `ManifestUnavailableError`); `pinned_manifest()` stays the static default (ADR 0004).
3. **`mcp/alpaca_order_schema.json`** — **pinned**: `money-pit pin-order-schema` (introspects
   `place_stock_order`) was run against the live server, replacing the stub and removing the sentinel,
   so the ADR-0007 gate is green. The pin's deferred reconciliations landed (ADR 0014): the
   `quantity→qty` rename and float→string param typing, with the `mcp/clients._order_arguments` shim deleted.
4. **`src/email_server/server.py`** — FastMCP `send_email(to, subject, body)`, no `money_pit` import
   (ADR 0009). The pipeline's own `EmailSender` is direct smtplib (`money_pit/email_sender.py`).

---

## Phase 8: `pipeline/recovery.py` — COMPLETE (ADR 0018)

Recovery reconciles the prior run's open orders as the **graph entry node**, before snapshot/planning.
It locates the most recent prior run's `execution_journal.json`, re-observes each potentially-open leg
(journal phase `SUBMITTED` / `PARTIALLY_FILLED`) at its current broker status via the injected fill
observer, and decides: any leg still open → **halt** the new run (double-exposure risk) + email
"money-pit: Recovery Halt - {slug}" (`recovery_router` routes recovery → END, nothing planned); an
abnormal-but-now-settled prior run (`EXECUTED_INCOMPLETE` or crashed `outcome=None`) → **notice + proceed**
(email "money-pit: Prior Run Reconciled - {slug}"); otherwise proceed silently. A `recovery.json` audit
artifact is written every run. The fresh snapshot already handles every *filled* leg, so recovery only
closes the open-order gap.

Recovery builds on the **real observed fills** journaled by the independent-order path (ADR 0017) — the
fill observer re-queries any potentially-open leg's current status. There is **no auto-unwind** (a stray
order is handed to a human; that policy is deferred with §15 #11 / the atomic-group work), and the
`COMPENSATION_FAILED` reconciliation branch stays a fail-closed stub, aligned with the deferred
atomic-group path.

New: `schemas/recovery.py` (`PriorRunReconciliation`, `ReconciledOrder`), the `RecoveryDecision` enum,
`reconcile_prior_run` + `make_recovery_node` in `pipeline/recovery.py`, `recovery_router` + the `HALT`
literal in `graph/edges.py`, the `recovery_decision` state key, and the `RECOVERY_JSON_FILENAME` constant.

---

# Remaining Work

The deterministic spine (Phases 1–4), the LLM cores (Phase 5), the source-adapter interface (Phase 6),
the MCP/email layer (Phase 7), the independent-order execution + real fill observation (ADR 0017), and
recovery (Phase 8) are complete and green. What remains splits into three tiers: **product surface not
yet built** (needed for autonomous real operation — Phases 9–10), a **deferred frontier** (fail-closed
stubs correctly gated on a real trigger — build only when that trigger exists), and a **hygiene backlog**.

Guiding constraint (no versions — build the final form): the deferred-frontier items are not "unfinished
v0s." They fail closed today and must stay stubbed until their real inputs exist (an interdependent
thesis; a second source); building them speculatively would be designing against nothing.

## Phase 9: Video multimodal ingestion (`adapters/video.py` Step 2)

**COMPLETE (code) — ADRs 0019–0022; live tier pending an owner run.** The real front-end now exists as a
new orchestration-owned `src/money_pit/ingestion/` subpackage that turns a YouTube URL into a real
`VideoPayload` the (already-built) `VideoAdapter` consumes:

- **Fetch + caption-first cascade** (`fetch.py`, `captions.py`): yt-dlp download; prefer uploader → auto
  captions → **faster-whisper** transcription only when captions are absent (ADR 0019, deviating from the
  spec's WhisperX — CTranslate2, no torch promotion). `TranscriptSource` + `has_word_timestamps` recorded
  honestly.
- **Keyframes + on-screen extraction** (`keyframes.py`, `on_screen.py`): PySceneDetect scene-change
  keyframes (capped by `keyframe_max_frames`) → **Claude multimodal VLM** via `pydantic_ai.BinaryContent`
  for on-screen text + chart-footer attribution (ADR 0020 — no native OCR). Feeds `on_screen_text` and,
  through the A1 prompt, per-claim `cited_sources`.
- **Fusion + caching + CLI** (`fuse.py`, `pipeline.py`, `__main__.py`): `ingest_video` wires the stages
  with per-stage artifact caching (ADR 0021, extending ADR 0002); `money-pit run <url>` / `ingest <url>`
  produce `signals/{source_id}.json` and drive `run_pipeline`. Packaged as a `video` optional extra with
  lazy heavy imports (ADR 0022). Every stage is an injected seam tested offline with fakes + cached
  fixtures; the `agent_1` prompt now admits on-screen attribution.

Remaining: run the opt-in `live_video` tier (`MONEY_PIT_LIVE=1 -m live_video`, needs the `video` extra +
GPU + Claude creds) against the pinned URL to validate the real download→transcribe→keyframe→VLM path
end-to-end. Deferred within Phase 9 scope: word-level narration↔frame *fusion* using the whisper word
timestamps (captions carry only segment timing; on-screen extraction keys off keyframe locators
independently, so this is a refinement, not a blocker).

## Phase 10: Scheduler / autonomous trigger

`run_pipeline` is invoked manually today; there is no scheduled trigger (`architecture.md` §13
APScheduler/cron, §16 build-order step 6). Needed for unattended daily operation. The open design
decision is **run trigger / cadence** (`architecture.md` §15 #13): new-episode detection (video anchors
cadence) vs. a fixed schedule vs. any-source arrival — lean is video-anchored at first. Resolve §15 #13,
then wire the scheduler around `run_pipeline` (which already owns slug/working-dir/recovery entry).

## Deferred Frontier (fail-closed stubs — gated on a real trigger)

Build **only** when the gating input exists; each fails closed today and must not be built speculatively.

- **Atomic-group compensation** — the `AtomicGroupNotSupportedError` terminus in `pipeline/execution.py`
  and the full §7a step-3 transactional model (all-or-nothing pre-flight, safe-order leg execution,
  realized-fill compensation, `COMPENSATION_FAILED` urgent escalation). Produces the currently-dead
  `PARTIAL_COMPENSATED` / `COMPENSATION_FAILED` outcomes and unblocks the `COMPENSATION_FAILED`
  reconciliation branch that recovery (ADR 0018) leaves stubbed. **Gated on:** a real interdependent thesis
  (a non-null `group_id`), which at N=1 A4 never emits (`design_decisions.md` §5), **and** the open design
  decisions `architecture.md` §15 #9 (grouping criteria), #10 (leg-execution ordering), #11 (compensation
  cost bound). Resolve those three before any code.
- **Multi-source corroboration (N>1)** — `agents/corroboration.py` is a no-op stub (always
  `ClaimRelations(agree=[], disagree=[])`); the aggregator's embedding-cluster + thin-LLM corroboration/
  conflict labelling and cross-source tier reconciliation are dormant. **Gated on:** a second source
  adapter existing. Open design: `architecture.md` §15 #14 (corroboration/similarity mechanism), #15 (tier
  reconciliation — confirmed max-tier, revisit if noisy), #16 (adapter trust weighting). `test_n1_stubs.py`
  turns red the moment a second source or interdependent thesis activates a dormant path — that is the
  signal to build this.

## Hygiene / Robustness backlog (do anytime)

- **Live schema-drift guard** — a `@pytest.mark.live` test diffing the pinned `mcp/alpaca_order_schema.json`
  against the live `place_stock_order.inputSchema`; nothing currently catches Alpaca schema drift until a
  real order rejects (flagged in the Watch Items above).
- **`factor_tags` / `correlated_overlaps`** — v0-deferred to empty in `alpaca_portfolio.py`, so A4's
  Step-3 overlap/factor constraint logic runs on empty inputs (degraded, not broken). Populate them (VLM/
  reference-data sourcing) to make the overlap/factor headroom clamps real.
- **Stale markers cleanup** — comments in `agents/research_tools.py` and `adapters/video_llm.py` still say
  "wired in Phase 7 / converge in Phase 7"; cosmetic, retire alongside the relevant phase.

---

## Key Checkpoints

| Checkpoint          | Criterion                                                                                                                  |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| schemas/ complete ✓ | `python -c "from money_pit import schemas"` clean; basedpyright clean; enum guard passes                                   |
| compute/ complete ✓ | All property tests green; every reachable regime tag covered; 96 tests passing                                             |
| gates complete ✓    | Three `graph/edges.py` gate tests pass on hand-built JSON                                                                  |
| spine green         | End-to-end paper-trade run with stub agents; all working-dir files schema-valid; zero LLM calls                            |
| schema pinned ✓     | real `mcp/alpaca_order_schema.json` pinned from live Alpaca MCP (stub sentinel removed); the fail-closed gate flipped green and `execution_params` validates against it |
| recovery gate ✓     | Recovery is the graph entry node (ADR 0018); a still-open prior order halts the run to END before any snapshot/planning, an abnormal-but-settled prior run emits a notice and proceeds, else silent proceed; `recovery.json` written every run |
| full pipeline       | End-to-end with real agents on paper-trading account; `determination.json` written; email or execution triggered correctly |
| video ingestion ✓ (code) | `ingestion/` subpackage turns a YouTube URL into a real `VideoPayload` → `SignalSet` (yt-dlp + captions/faster-whisper + PySceneDetect + Claude VLM); all stages green offline (ADRs 0019–0022). Live end-to-end validation (`-m live_video`, needs the `video` extra + GPU) pending an owner run |
| scheduler           | An unattended scheduled trigger runs the pipeline on cadence without manual invocation (Phase 10, §15 #13 resolved)         |

---

## Watch Items

- **`src/money_pit/mcp/alpaca_order_schema.json`**: **pinned** from the live Alpaca MCP server (`money-pit pin-order-schema`, ADR 0008); the stub sentinel is removed, so the ADR-0007 fail-closed gate is green and `test_load_order_schema_default_path_is_pinned` asserts it stays pinned. The two deferred `ExecutionParameters` reconciliations the pin forced have landed (ADR 0014): the `quantity → qty` rename (the transitional `mcp/clients._order_arguments` shim is deleted; the write client submits `to_order_payload()` directly) and float → string param typing, with the `InvalidExecutionAmountError` amount guard added (ADR 0015). The per-test stub opt-in scaffolding (`stub_free_order_schema_path` fixture and the `order_schema_path`/`schema_path` injection seams) has been **retired** — ADR 0007's terminus reached — leaving only `load_order_schema(path=)`; ADR 0016 records the related `pinned_manifest` error-propagation simplification that fell out of it. **Remaining follow-up:** a live-tier schema-drift guard comparing the pinned artifact to the live `inputSchema`.
- **Write-client boundary discipline** (ADR 0008): Alpaca has no key-level read/write split, so the "only execution places orders" guarantee now rests on **client separation** (alpaca-py reads, MCP writes) and constructing `AlpacaWriteDeps` only on the execution path — no type prevents a future caller from breaching it. Snapshot reads must stay on alpaca-py, never a `trading`-scoped MCP instance.
- **N=1 stub inertness** (`test_n1_stubs.py`): turns red when a second source or the first interdependent thesis activates a dormant aggregation path.
- **`terminal_state_router`**: routes `None`→"validate", `NO_ACTION`→"no_action", else→"notify"; the 4-member `TerminalState` (ADR 0006) is authoritative — confirm against `pipeline_contracts.md` §0/§6a when touching it.

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
│   │   └── test_execution_params.py     # notional formatting, payload validates, amount-guard rejections
│   └── graph/
│       └── test_edges.py                # three gates, hand-built JSON
├── integration_tests/
│   └── pipeline/
└── acceptance_tests/
    └── paper_trade/
```
