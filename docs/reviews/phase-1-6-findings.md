# Phases 1-6 Conformance Review — Findings

**Status:** review complete; remediation **complete** (waves P1, P2, S1–S8 landed, each
reviewed and green — ADRs 0003–0006 record the design decisions). Deferred by owner:
config.py → pydantic-settings (item J) and the repo-wide ruff debt / I001·D101·BLE001·RUF022
(item K). Minor noted follow-ups: `notification._build_subject`'s now-vestigial `_validation`
parameter; and the two Phase-7 bounded stubs (atomic-group execution, live MCP manifest
introspection) whose red-when-activated tests already stand guard.
**Method:** five parallel `python-reviewer` batches (schemas, compute, graph+pipeline,
agents+adapters, tests) against `docs/architecture.md`, `docs/pipeline_contracts.md`,
`docs/design_decisions.md`, `docs/package_structure.md`, plus two targeted verifications
(`action_steps.json` serialization; `TerminalState` enum vs architecture §10).

The architecture is sound and the read/write safety boundary holds by construction (only
`execution` receives an order callable; no pipeline node imports another). But phases 1-6 —
broad-stroke generated before the specialized Python agents existed — eroded the
**determinism and auditability** principles under implementation pressure. Every theme below
is an instance of that erosion. Nothing live-trades incorrectly _today_ (execution is a
paper stub; the Alpaca schema keeps the path half-gated), but the spine cannot yet be
trusted with capital: it has no failure-mode tests, and two safety-critical nodes report
success they have not earned.

Fixes are sequenced by **capital proximity**, restore-to-spec by default, each wave led by
the failure-mode test that proves the node fails closed.

---

## Cross-cutting themes

### T1 — The draft→node→contract LLM-boundary pattern was never applied

`[BLOCKER-class · schemas, compute, agents]`

A2/A3/A4 agents declare the on-disk _contract_ type as `output_type`, so the model authors
deterministic fields: `confidence` (§9: code-derived), `Q###` ids, `data_sources` routing,
carry-through `category`/`signal_source`/`signal_tier` joins, plus draft-schema fields
`requires_validation`, `has_actionable_content`, `source_context`. The purpose-built
`DraftQuestion` / `AnswerDraft` exist but are unused.

- `agents/answer_synthesis.py:38-43` — `output_type=list[Answer]`; LLM authors `confidence`.
- `agents/claim_questions.py:24-27` — `output_type=list[Question]`; LLM authors `id`,
  `data_sources`, `signal_tier`.
- **Downstream bug — `compute/confidence.py:4-16`:** `_PRIMARY/_SECONDARY/_TERTIARY` are bare
  strings duplicating `DataSourceToken`, matched against free-text `Answer.sources_used`
  (e.g. `"FRED (DGS10)"`). The set intersection never matches → **every answer silently
  collapses to MEDIUM**, corrupting the sizing haircut. Type `sources_used` as
  `DataSourceToken` at the boundary and match on the enum.

**Fix:** switch agents to draft output types; derive the deterministic fields in nodes.

### T2 — Required audit artifacts never written; A6 reads memory not files

`[MAJOR · graph, pipeline]`

`determination.json/.md` (the §6.7/§12 audit anchor), `analysis.md` (full A4 reasoning incl.
every dropped claim), and `aggregated_signals.md` are never produced. `determination_gate`
(`graph/edges.py:35-40`) reads `validation_steps` from graph state instead of the persisted
`action_steps_validation.json`, violating file-based replayability (§4/§5).

**Fix:** A6 reads the file and writes `determination.json/.md`; nodes emit the missing
`.md`/`analysis.md` companions.

### T3 — Terminal-state routing: 2 outcomes where the spec needs 4

`[MAJOR · graph/edges.py, schemas/enums.py, pipeline/analysis.py]`

Post-processor `NO_ACTION` routes to `notification` (emails) while signal-gate `NO_ACTION`
routes silently to `END` — same state, two behaviors. `VALIDATION_ERROR` /
`ORCHESTRATION_ERROR` terminals are never set/unreachable; parse-failure collapses into the
validation-error email branch. In schemas: `TerminalState` (`enums.py:112-119`) has
`VALIDATION_FAILED` (canonical is **`VALIDATION_ERROR`**, architecture §10), omits
**`ORCHESTRATION_ERROR`**, and carries `EXECUTION_FAILED` (an execution _outcome_ per §0, not
a §10 terminal state). The §6a halt object is unrepresentable (`ActionStep.step_failed` is
`Literal[None]`; `AnalysisJudgment` has `claim_id`, not the `step_id` the router keys on).

**Fix:** reconcile the enum; model the halt object explicitly; give the router quiet-NO_ACTION
and parse-failure→ORCHESTRATION_ERROR branches.

### T4 — The two safety-critical nodes are stubs that misreport state

`[MAJOR · pipeline/execution.py, pipeline/validator.py]`

- `execution.py:34-63` places every order in a plain loop, writes the journal **once at the
  end** (a mid-loop crash leaves _no_ record — defeats the §7a incremental-journal
  guarantee), and hardcodes `outcome=EXECUTED_CLEAN` even if a leg is rejected. No terminus
  marker.
- `validator.py` (A5) has its tool-existence check stubbed (`mcp/manifest.py` empty) and
  takes an injected LLM `behavioral_match` predicate — contradicting the pinned "A5 contains
  no LLM" (§6.6/§9/§11.2), no ADR, defaulting to always-`True`. So A5 today is jsonschema +
  `client_order_id` only, silently green on checks it isn't performing.

**Fix (capability vs. dependency line):** build the independent path now — incremental
journaling + honest outcome derivation (execution); revert the predicate to a `tool_map`
config lookup (validator). Leave the genuinely Phase-7-dependent pieces as **explicitly
marked, fail-closed** bounded stubs: atomic-group compensation (unreachable at N=1, with a
test that flips red when `group_id` populates); the manifest existence check (unverifiable →
UNMATCHED/HALT, never a silent pass). _A safety gate that cannot run its check must not
answer "safe."_

### T5 — Production entrypoint silently mixes real and stub dependencies

`[MAJOR · pipeline/orchestration.py:306-309]`

`run_pipeline` defaults `fetch_portfolio`/`place_order`/`send_email` to Phase-4 stubs (fake
$100k empty account, paper placer, no-op email) _while constructing real LLM agents_. A
caller injecting a real `place_order` but forgetting `fetch_portfolio` would size real orders
against a fabricated account, silently.

**Fix:** production entrypoint requires real deps; stub wiring moves behind the explicit
`phase4_overrides()` test path.

### T6 — Alpaca schema single-source is wrong, misplaced, and silently masking Phase-7 gate 1

`[MAJOR · compute/execution_params.py:9-11, pipeline/validator.py:20-22]`

Both modules independently compute a 4-`.parent` path → `<repo-root>/mcp/alpaca_order_schema.json`
(docs pin `src/money_pit/mcp/`; the repo-root location won't ship in the wheel). They agree
only by duplicated literal. A stub file _actually exists_ at that wrong location, so the
intended "CI-red until pinned" gate is **silently green**, and `execution_params` loads the
schema then discards it, emitting hardcoded keys it never validates against.

**Fix:** one `ALPACA_ORDER_SCHEMA_PATH` in `money_pit.mcp` imported by both; move the file
under the package; validate/derive emitted keys from the loaded schema.

### T7 — Scattered magic values / un-centralized cross-node string contracts

`[MINOR (several) · compute, pipeline, agents, config]`

The `indicator:` prefix + five indicator names triplicated across `questions`/`retrieval`/
`analysis`; `_MAX_KELLY_FRACTION`, `"anthropic:"`, the 4-`.parent` prompt path, `n_results=5`,
`"market"`/`"day"` unnamed/duplicated; `config.py` hand-rolls env parsing (§13 says
`pydantic-settings`), duplicates every default, is the one non-frozen model. Also: portfolio-gap
answers label `alpaca_mcp` as source but fetch via yfinance (mislabels provenance → corrupts
confidence + audit); `questions`/`retrieval` swallow agent failures with bare `except → []`
and no log (violates "fail closed, notify loudly"); notification subject + working-dir root
(`runs/` vs `data/daily_show/`) drift from pinned conventions; `episode_summary` should be
`summary`; loose `str` where enums belong; placeholder `data_sources: ["none"]` unrepresentable.

_Latent trap (not an active bug):_ the `ActionSteps` object-wrapper (`schemas/action_steps.py:45`)
would emit `{steps:[...]}` and make A5 HALT — but the node serializes a list directly
(`analysis.py:204-205`), so the wrapper is dead. Retire it so no one uses it later.

---

## Phase-7 readiness & test-debt (the five gates)

| Gate                                 | Status                            | Note                                                                                                                                                                                                                                                                   |
| ------------------------------------ | --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1/3 · order-schema pin**           | ❌ broken & masked                | Wrong path, misplaced outside `src/`, silently green, duplicated, emission never validated against it. Fix in P1/T6 before Phase 7.                                                                                                                                    |
| **3 · manifest ↔ A5 agreement**      | ❌ not ready                      | `manifest.py` empty; A5 tool-existence stubbed; behavioral-match an LLM no-op. The 4→5→6 literal seam can't be verified end-to-end yet.                                                                                                                                |
| **4 · ResearchTools ↔ ResearchDeps** | ⚠️ clean port, unmanaged terminus | Satisfiable structurally today, but convergence is only a docstring promise. Make the protocols the single shared source both `agents/` and `mcp/clients.py` import; add a `@runtime_checkable` conformance test so a Phase-7 rename fails a test, not silently forks. |
| **2 · read/write negative test**     | ✅ intact / ❌ untested           | Boundary safe by construction; no negative test asserts the read path can't reach `place_order`. Author at Phase 7.                                                                                                                                                    |
| **5 · email_server isolation**       | ❌ untested                       | No import-graph check. Author at Phase 7 (import-linter / module-graph test).                                                                                                                                                                                          |

---

## Testing architecture findings

- The entire capital-moving `pipeline/` layer has **no direct unit tests** — reachable only
  through one happy-path e2e (`test_paper_trade_e2e.py`), itself overloaded (many assertion
  axes) and never driving the HALT/NOTIFY branches (so `notification.py` is fully uncovered).
- **Zero `pytest.raises` in the suite** — not one failure mode is pinned, despite nearly
  every node raising on missing state. This is the single most damning gap.
- Rich private helpers untested (`validator._validate_step`'s five outcomes,
  `analysis._extract_macro_indicators`, `video._to_claim`, journal building,
  `notification._build_subject/_build_body`).
- Smaller: video-adapter fixture sprawl (3 near-identical stubs → one indirect-parametrized
  fixture); `test_execution_params` covers only buy/sell happy paths and the CI-red intent is
  silently green; a few unit-scoped tests mis-tiered under `integration_tests/`.
- **Strongest asset (keep and extend):** zero mocking; real dependency injection via stub
  callables (`PipelineOverrides`, adapter `agent=`).

## Coverage-gap inventory (prioritized by capital proximity)

- **HIGH:** `pipeline/execution.py`, `pipeline/analysis.py`, `pipeline/validator.py`,
  `compute/execution_params.py`, `compute/tool_map.py` (the `COMPENSATING_ACTION` reversal
  map), `pipeline/aggregator.py`, execution-boundary schemas (`action_steps`, `journal`,
  `validation_results`).
- **MED:** `questions`, `retrieval`, `notification`, `snapshot`, `orchestration` (not the
  live-HTTP `_Direct*Tools`), `graph/graph.py` terminal branches.
- **LOW:** `routing`, `video_llm`/`base` schema round-trips, `corroboration` stub, remaining
  pure schemas, `config`, `constants`.
- **Deferred (not gaps):** `mcp/clients`, `mcp/manifest`, `email_server/server`,
  `pipeline/recovery` (Phase 7/8 stubs).

---

## Decisions log

- **Restore-to-spec by default.** Drift reverts unless there's a concrete reason the spec is
  wrong; drifted code existing is not a reason. The three named reversals are straight reverts
  (no ADR): A5's LLM predicate, A4's list-vs-`AnalysisJudgment`-container, A6 graph-state read.
- **Conformance now, Phase-7 deferred**, along the capability-vs-dependency line. Every
  bounded stub fails closed or announces "not yet real" — never defaults to the passing answer.
- **Test-first per wave:** the failure-mode test (fail-closed proof) is the first change; the
  fix makes it green. DoD = the failure path is proven.
- **Sequence by capital proximity:** execution → validator → analysis → execution_params →
  tool_map, then audit/routing, entrypoint, draft-remainder, fills.
