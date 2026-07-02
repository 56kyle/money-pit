# Execution journal semantics at the paper-stub wave (pre-Phase-7)

- Status: accepted
- Date: 2026-07-01
- Deciders: owner, architecture author

## Context and Problem Statement

`pipeline/execution.py` submits validated orders and records `execution_journal.json`. Until
the real Alpaca write integration lands (Phase 7), the injected
`place_order: Callable[[ExecutionParameters], str]` returns only a broker order id — a
successful call proves *"submitted"*, not *"filled"*. The pre-remediation node wrote the
journal once at the end, hard-coded every entry `phase=SUBMITTED` and the run
`outcome=EXECUTED_CLEAN`, and did not handle submission failure — so a mid-loop crash left no
record and a rejected leg was still reported as clean success (`docs/reviews/phase-1-6-findings.md`
T4).

At N=1 every step is independent (`group_id` always null; `docs/design_decisions.md` §5); the
atomic-group transactional model (`docs/pipeline_contracts.md` §7a) is the deferred stub. Three
decisions are needed to make the independent path honest and crash-survivable now, without
pulling Phase-7 broker detail forward.

Governing principle: **a stub must fail closed or announce "not yet real" — never default to
the passing/happy answer.**

## Decision Drivers

- Fail-closed: never report unearned success (a fill we cannot observe, a clean outcome we did
  not complete).
- Auditability & recovery: a crash must leave a truthful partial record; Phase-8 recovery reads
  an incomplete journal.
- Capability vs. dependency: build what needs nothing from Phase 7; leave genuinely
  Phase-7-dependent pieces as explicitly-marked bounded stubs.
- Determinism and granular testability.

## Decision Outcome

**1. `EXECUTED_CLEAN` is redefined, pre-Phase-7, as "all independent legs submitted without
exception" — not "filled."** Entries stay `phase=SUBMITTED` (never `FILLED`, which would be
unearned); `filled_qty` / `filled_avg_price` / `realized_notional` stay `None` as explicit
not-yet-real markers. This keeps the paper pipeline able to reach a success terminal for
dry-run testing while making the narrowing honest on the face of the record. An empty plan
(zero action steps) is vacuously `EXECUTED_CLEAN`: a no-trade day is caught upstream by the
`NO_ACTION` terminal, so execution never derives an outcome from zero real steps.

**2. `ExecutionJournal.outcome` becomes nullable (`ExecutionOutcome | None`).** `None` = no
terminal verdict yet — the value during incremental writes and the value left by a mid-run
crash. A terminal member is written only at clean completion. Absence is the truthful
representation of "incomplete"; it never fabricates a premature `EXECUTED_CLEAN`.

**3. A non-null `group_id` fails closed by raising `AtomicGroupNotSupportedError` before any
`place_order` call.** Executing one leg of an all-or-nothing group would leave exposure nobody
chose. This is the marked terminus of the atomic-group stub.

### Consequences

Good: the journal is honest and crash-survivable; the paper pipeline can complete; the atomic
branch cannot silently mis-execute; the Phase-7 upgrade path is clean (fills observable →
`phase=FILLED`, `filled_*` populated, `EXECUTED_CLEAN` regains its filled meaning,
`PARTIAL_COMPENSATED`/`COMPENSATION_FAILED` become reachable).

Bad / to watch: `EXECUTED_CLEAN` is submission-level pre-Phase-7 — a reader must not assume
fills (mitigated by `phase=SUBMITTED`, `None` fills, and this ADR). Nullable `outcome` adds a
`None` branch the §7 outcome→`TerminalState` mapping must handle explicitly (reject/flag an
incomplete journal) when that mapping is built (wave S5).

### Confirmation

Failure-mode tests pin each decision: crash mid-loop (unexpected exception) → on-disk journal
holds the completed-so-far entries with `outcome=None`; a broker submission failure
(`OrderSubmissionError`) → that leg `phase=FAILED` and the loop continues, run
`outcome=EXECUTION_FAILED`; a step with a non-null `group_id` → `AtomicGroupNotSupportedError`
and `place_order` never invoked (this test flips red when Phase 7 implements atomic groups —
the signal the stub went live).

## Considered Options (key rejections)

- **Outcome representation:** a new `IN_PROGRESS` enum member (pollutes an all-terminal enum and
  forces every mapping to handle a non-terminal value) and a pessimistic `EXECUTION_FAILED`
  default upgraded at completion (cannot distinguish "crashed at leg 3/5" from "genuinely
  failed") were both rejected in favor of nullable `outcome`.
- **`EXECUTED_CLEAN` semantics:** keeping the run non-terminal until Phase-7 fill confirmation
  was rejected — it blocks the pipeline from ever completing an execution-success path on paper.
- **Atomic-group handling:** journaling `SKIPPED`/`PREFLIGHT_FAILED` was rejected — those phases
  assert a feasibility pre-flight we have not implemented; a typed raise honestly says "cannot
  handle."

## More Information

Supersedes the hard-coded `EXECUTED_CLEAN` / end-of-loop journal write flagged in
`docs/reviews/phase-1-6-findings.md` T4. Related: `docs/pipeline_contracts.md` §7/§7a,
`docs/design_decisions.md` §5.
