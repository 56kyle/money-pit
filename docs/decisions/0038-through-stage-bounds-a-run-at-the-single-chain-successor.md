---
status: accepted
date: 2026-07-26
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# `--through <stage>` Bounds a Run at the Single Successor in the Linear Planning Chain

## Context and Problem Statement

ADR 0035 gave `run_pipeline` a `stop_before_execution: bool` that compiles the graph with
`interrupt_before=[EXECUTION_NODE]`. ADR 0036 factored the composition root into per-dependency
builders, and ADR 0036's terminus — `pipeline/stages.py` — made a single node runnable standalone
against an existing run directory. Neither was reachable from the CLI: plan-only mode was exercised
only from the opt-in live pytest tier, and `run_stage` had no operator-facing entrypoint at all.

The operator wants to run the pipeline manually against the paper account and see what *would*
happen without placing orders. That needs two CLI affordances — a bounded chain run and a
single-stage run — and the bounded run needs a precise answer to a question the boolean never had to
ask: given a named stop point, which nodes should the graph be interrupted before?

## Decision Drivers

- Full runs with no bound must compile and behave exactly as they do today.
- ADR 0035's full-fidelity property must survive: a bounded run is a *fidelity* instrument, so it
  must not suppress production side effects the real run would have performed.
- `--through determination` must be exactly the old `stop_before_execution=True`, or the ADR 0035
  live tier no longer pins what it claims to pin.
- One mechanism, not two. A boolean and a stage bound coexisting as independent parameters would let
  a caller name both and leave the resolution order undefined.

## Considered Options

- Interrupt before the single successor in the linear planning chain
- Interrupt before every node downstream of the bound
- Keep `stop_before_execution` alongside a new `through`

## Decision Outcome

Chosen option: **interrupt before the single successor in the linear planning chain**, with
`stop_before_execution` **removed** rather than kept.

`interrupt_before_successor_of(through)` maps a `Stage` to a one-element interrupt list by position
in `PLANNING_CHAIN`:

```
recovery → snapshot → aggregator → questions → retrieval → analysis → validator → determination → execution
```

`--through determination` therefore yields `["execution"]`, which is precisely the ADR 0035 compile
path. `None` yields `[]`, and `build_graph` compiles without a checkpointer exactly as before.

Interrupting only the successor is the load-bearing choice. The graph is not a line: `analysis`
routes to `notification` or `no_action_terminal` as well as `validator`, and `determination` routes
to `notification` or `finalizer` as well as `execution`. Interrupting before every downstream node
would sever those branches, so a bounded run that hit `ANALYSIS_HALT` would fail to send the owner
email a real run sends — the mode would then be verifying something other than production, which is
the failure ADR 0035 explicitly refused when it declined to suppress mail. Bounding only the linear
successor leaves every notification and terminal branch reachable: the run stops when it would have
carried on planning, and otherwise concludes as it always would.

### Why the chain lives in a new leaf module

`Stage` moved from `pipeline/stages.py` to a new `pipeline/chain.py`, which also owns
`RECOVERY_NODE`, `EXECUTION_NODE`, `PLANNING_CHAIN` and the mapping function.

The forcing constraint is import direction. `build_graph` does the translation, so `graph/graph.py`
must see `Stage`; but `pipeline/stages.py` imports `pipeline/orchestration.py`, which imports
`graph/graph.py`. Leaving `Stage` in `stages.py` would close that cycle. A leaf module holding the
node identities and their order — depending on nothing else in the package — is importable from both
sides.

It is also the better home on its own merits. `Stage` is an enum of node identities and their
ordering; `stages.py` is about *constructing* one node's dependencies and checking its inputs exist.
The chain deliberately spans nodes that are not stages (`recovery` and `execution` are both in the
chain, neither is standalone-runnable per ADR 0036), which is the clearest sign the two concepts were
not the same thing.

`PLANNING_CHAIN` is written out as an independent literal rather than derived from `Stage`'s
declaration order. This is the ADR 0035 lesson applied again: a derived list silently absorbs a newly
inserted node and catches nothing, whereas a literal forces whoever adds a node to decide
consciously where in the chain it sits.

### Consequences

- Good, because `money-pit run --through determination` and `money-pit stage <name> --run-dir <dir>`
  put both halves of the four-phase effort in the operator's hands, and neither can place an order.
- Good, because `stop_before_execution` is gone rather than retained, so there is exactly one way to
  bound a run and no undefined interaction between two.
- Good, because `run` and `analyze-text` now echo the run directory unconditionally. A bounded run's
  deliverable *is* the directory; printing it only when bounded would make the operator learn two
  output shapes.
- Bad, because `run --through` and `stage` overlap: `--through snapshot` and `stage snapshot` both
  produce a portfolio snapshot. They are genuinely different operations — the former creates a run
  directory and passes through recovery, the latter demands an existing one and touches nothing else
  — but an operator has to know which they want.
- Bad, because a `Stage` absent from `PLANNING_CHAIN` is a programming error the type system cannot
  catch, since the chain is an independent literal rather than a derivation. Total-mapping
  enforcement at import was rejected as machinery disproportionate to a nine-element literal one
  file away from the enum. The mitigation is a test-time invariant rather than a runtime one: a
  drift guard asserts every `Stage` appears in the chain, so the error surfaces in CI, not at an
  operator's console. `PlanningChainError` remains as the runtime backstop.
- Neutral, because the returned state of a bounded run still carries the pre-pause values ADR 0035
  flagged as a trap for state-based assertions. Nothing here changes that; artifacts remain the
  thing to assert on.

### Confirmation

The bound is pinned at every hop between the command line and the compiled graph, because a
`through` silently dropped anywhere along it means an operator who asked for a bounded run gets a
real execution:

- `tests/unit_tests/pipeline/test_chain.py` — `interrupt_before_successor_of` for every stage against
  a literal table (not derived from `PLANNING_CHAIN`, which would absorb a chain change and catch
  nothing), `None` yielding `[]`, and the drift guard that every `Stage` is chained.
- `tests/unit_tests/test_main.py` — CLI parse through `_run_url` and `_run_signals_dir` to
  `run_pipeline`, parametrized over every stage plus the omitted-`None` case, for both `run` and
  `analyze-text`.
- `tests/unit_tests/graph/test_graph.py` — `build_graph` compiles under every bound, plus a
  correspondence check that every `PLANNING_CHAIN` name is a registered node. This closes the one
  hop where the chain's strings meet the graph's node names, which no type relates.
- `tests/integration_tests/pipeline/test_paper_trade_e2e.py` and
  `tests/integration_tests/paper_trade_live/test_plan_only_live.py` pass `through=Stage.DETERMINATION`
  where they passed `stop_before_execution=True`, and their existing pins — `completed_steps` equal
  to the independent `_PLAN_ONLY_PATH_STEPS` literal, no execution journal, no determination report,
  planning artifacts present and schema-valid — carry over unchanged. That equivalence is the
  evidence the generalization preserved the old behavior exactly at the one bound that had a name
  before.

Both drift guards were verified to fire by pointing a `PLANNING_CHAIN` entry at a non-existent node
and confirming the compile, the `PlanningChainError` path, and the correspondence check all went
red.

## Pros and Cons of the Options

### Interrupt before the single chain successor

- Good, because notification and terminal branches stay reachable, preserving ADR 0035 fidelity.
- Good, because the identity with `stop_before_execution=True` at `determination` is exact, so the
  existing pins transfer without reinterpretation.
- Bad, because it requires an explicit ordering the graph topology does not otherwise state.

### Interrupt before every downstream node

- Good, because "stop after X" is enforced structurally with no ordering to maintain.
- Bad, because it severs the notification branches, so a bounded run would suppress owner mail a real
  run sends. Decisive against — it reintroduces exactly what ADR 0035 rejected.

### Keep `stop_before_execution` alongside `through`

- Good, because no caller changes.
- Bad, because two parameters expressing one concept invite contradictory arguments with no defined
  resolution, and the boolean is strictly the weaker of the two.

## More Information

The pause mechanism, the honesty-of-artifacts argument for an interrupt over a reroute, and the
decision not to build resumption are all [ADR 0035](0035-plan-only-runs-pause-before-execution.md).
The per-dependency builders that make a single stage constructible are
[ADR 0036](0036-per-dependency-builders-as-the-composition-root.md), which also fixes why `recovery`,
`notification` and `execution` are not stages. Resuming a bounded run into a real execution remains
deliberately unbuilt.
