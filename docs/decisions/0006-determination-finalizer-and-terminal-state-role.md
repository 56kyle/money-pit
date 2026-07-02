# Determination finalizer node and TerminalState as a routing marker

- Status: accepted
- Date: 2026-07-02
- Deciders: owner, architecture author

## Context and Problem Statement

Wave S5 addresses T2 (audit artifacts) and T3 (terminal-state routing) from
`docs/reviews/phase-1-6-findings.md`. Today A6 is a pure in-memory `determination_gate`
conditional edge (`graph/edges.py`) that reads `validation_steps` from graph state and returns
a route string — it never writes `determination.json/.md` (the §6.7/§12 audit anchor) and does
not read the persisted `action_steps_validation.json` (violating file-based replayability). The
routing also implements only two outcomes where §6a/§10 need four, and the two `NO_ACTION`
paths diverge (signal-gate → silent END; post-processor-empty `[]` → emails). `TerminalState`
has drifted: `VALIDATION_FAILED` (canonical `VALIDATION_ERROR` per §10, verified), no
`ORCHESTRATION_ERROR`, and it carries execution-*outcome* members (`EXECUTED_CLEAN`,
`PARTIAL_COMPENSATED`, `COMPENSATION_FAILED`, `EXECUTION_FAILED`) that §10 does not list as
terminal states.

The structural tension: `determination.json` wants both the pre-execution **decision**
(PROCEED/HALT — must be known to route) and the post-execution **outcome**
(`sub_agent_outcome`/`sub_agent_error` — only known after execution/notification runs). A
single pre-fork edge can satisfy neither the write nor the outcome.

## Decision Drivers

- §6.7/§12: `determination.json` records the recomputed go/no-go, failing step IDs, the spawned
  sub-agent, and its terminal outcome — a single honest audit artifact read from files.
- Fail-closed: never fabricate a premature success; an incomplete journal is a failure.
- The e2e invariant: the execute path leaves `terminal_state is None` (outcome lives in the
  journal), evidence that `TerminalState`'s real job is pre-execution routing.

## Decision Outcome

**1. Split A6 into a pure decision + a single post-rejoin finalizer.** A pure
`recompute_determination(ActionStepsValidation) -> Determination` (all `MATCHED` → PROCEED;
any `UNMATCHED` → HALT) runs against the **persisted** `action_steps_validation.json`; a
missing/empty/non-array `steps`, unreadable file, or unknown status literal raises a typed
`DeterminationParseError` → `ORCHESTRATION_ERROR` (no sub-agent, per §6.1). A single
**finalizer node**, reached after execution / the validation-error notification / the
parse-failure path rejoin, writes the complete `determination.json/.md` **once** — it can see
the journal outcome. This deviates from the literal "A6 = conditional edge" wording (§6.7/§9)
but is forced by §6.7/§12's own requirements (write the artifact, read the persisted file, record
the sub-agent outcome), which an edge structurally cannot satisfy. `determination.json` is
written **only on A6 paths** (real steps); `NO_ACTION`/`ANALYSIS_HALT` skip A6 per §10 and
produce none.

**2. `TerminalState` becomes a pre-execution routing marker — 4 members:**
`{NO_ACTION, ANALYSIS_HALT, VALIDATION_ERROR, ORCHESTRATION_ERROR}`, all set before the
execution fork. Execution-derived outcomes live solely in `ExecutionOutcome`/the journal
(the execute path leaves `terminal_state is None`). The `VALIDATION_FAILED → VALIDATION_ERROR`
rename and the `ORCHESTRATION_ERROR` add are straight restore-to-spec; **removing** the four
execution-outcome members from `TerminalState` is the modeling call recorded here.

**3. Four-way routing (§6a/§10):** `NO_ACTION` — both the signal-gate path and the
post-processor-empty `[]` path route to the SAME quiet terminal (no email); `ANALYSIS_HALT` →
notification (halt email) → END; `VALIDATION_ERROR` → notification (validation-error email) →
finalizer; PROCEED → execution → finalizer; `ORCHESTRATION_ERROR` → finalizer (no sub-agent).

**4. Fail-closed outcome mapping.** `map_execution_outcome`: `EXECUTED_CLEAN` /
`PARTIAL_COMPENSATED` → `success`; `COMPENSATION_FAILED` / `EXECUTION_FAILED` / **`None`
(incomplete journal, per ADR 0003)** → `failure`. `DeterminationReport.sub_agent_outcome` is
typed `Literal["success", "failure"] | None` (not a loose `str`). A post-PROCEED
`EXECUTION_FAILED` is recorded as `sub_agent_outcome=failure`; a dedicated failure *email* is
deferred to the notification wave (out of S5 scope).

### Consequences

Good: `determination.json` is a single honest write with the real sub-agent outcome; the
recompute + mappings are pure functions pinnable directly; the four outcomes are distinct; the
`NO_ACTION` divergence has one implementation; `ORCHESTRATION_ERROR` is genuinely reachable via
a typed exception. Bad / to watch: the graph gains a finalizer join (execution / notification /
parse-error → finalizer → END), a real topology change; a post-PROCEED execution failure is
currently record-only (no email) until the notification wave.

### Confirmation

Tests: `recompute_determination` over all-MATCHED / any-UNMATCHED; `DeterminationParseError`
on malformed validation input → `ORCHESTRATION_ERROR`; `map_execution_outcome` incl.
`None → failure` and `EXECUTION_FAILED → failure`; the finalizer writes `determination.json`
with the correct `determination`/`failed_steps`/`sub_agent_spawned`/`sub_agent_outcome`; the
post-processor-empty path reaches the quiet `NO_ACTION` terminal (no email); `terminal_state`
enum has exactly the four members (enum-coverage guard updated).

## More Information

Restores §10 terminal-state names; consumes the `AnalysisHalt` defined in ADR 0005 (S5 never
redefines it). Related: `docs/architecture.md` §6.6/§6.7/§10, `docs/pipeline_contracts.md`
§6/§6a/§7, ADR 0003 (nullable journal outcome, deferred this mapping to S5).
