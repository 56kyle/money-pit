---
status: accepted
date: 2026-07-26
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Plan-Only Runs Pause Before Execution Rather Than Terminating

## Context and Problem Statement

The pipeline was all-or-nothing. `money-pit run`, `analyze-text`, and `run-latest` all funnel into
`run_pipeline`, which builds the full graph and invokes it start to finish, including the execution
node. The only stage runnable in isolation was `money-pit ingest`. There was no dry-run flag, no
plan-only mode, and no approval gate anywhere in the codebase.

That left no way to answer the question an operator most wants answered before trusting the system
with capital: *what would this run actually do?* The existing live paper tier
(`tests/integration_tests/paper_trade_live/`) runs the real thing end to end, but it places paper
orders to do it — so observing the plan required also executing it.

The architecture is well suited to stopping partway. Every node reads its inputs from files in
`working_dir` and writes its outputs back there; `PipelineState` carries only `slug`, `working_dir`,
and routing flags, no domain data. Each stage is therefore close to a pure function of a run
directory, and the artifacts are already the handoff format a human would inspect.

How should a run stop short of execution, and what should it record about having done so?

## Decision Drivers

- **No artifact may lie about what happened.** The run directory is the audit trail for decisions
  that move capital; a record asserting execution occurred when it did not is worse than no record.
- The mode exists for manual operator verification and is invoked deliberately. It is strictly
  *safer* than the full run it replaces for that purpose — it removes the order path rather than
  adding one.
- Production behavior must be untouched. A verification affordance that perturbs the real path
  defeats its own purpose.
- The planning artifacts through `action_steps.json` must all be produced, since they are precisely
  what the operator inspects.

## Considered Options

- Pause with a LangGraph interrupt before the execution node
- Reroute the determination router's `EXECUTE` branch to the finalizer
- Inject a `place_order` override that refuses to place
- Add a new `TerminalState` for "planned, not executed"

## Decision Outcome

Chosen option: **pause with a LangGraph interrupt**. `build_graph` takes a keyword-only
`stop_before_execution: bool = False`; when set, it compiles with an `InMemorySaver` checkpointer and
`interrupt_before=[EXECUTION_NODE]`. `run_pipeline` takes the same flag and forwards it. When the
flag is false the compile path is unchanged; the one production-visible change is at the invoke
site, covered below.

The deciding argument is honesty of the persisted record. `determination_node`
(`pipeline/determination.py:112-119`) sets `sub_agent_spawned="execution"` on the `PROCEED` path
*before* execution runs — that key is set in anticipation, and the finalizer later resolves the
actual outcome from the execution journal. Any design that lets the finalizer run without executing
therefore writes a `determination.json` claiming execution was spawned with outcome `none`. An
interrupt sidesteps this entirely: the finalizer never runs, so no determination report is written
at all. A paused run is *unfinished*, which is exactly what it is.

This is also why **no new `TerminalState` value was added**. `NO_ACTION`, `ANALYSIS_HALT`,
`VALIDATION_ERROR`, and `ORCHESTRATION_ERROR` all describe runs that *reached a conclusion*. A
plan-only run reached no conclusion; it was deliberately stopped. Modelling "stopped early" as a
terminal state would put a non-outcome into an enum of outcomes, and every consumer of that enum
would have to learn to ignore it.

A checkpointed graph requires a thread id at invoke. `run_pipeline` passes one unconditionally,
keyed on the run slug: a thread config on an uncheckpointed graph is accepted and ignored, so this
avoids a branch at the invoke site for no behavioral cost.

### Where it lives, and the human-only property

The mode is exercised from the opt-in live tier
(`tests/integration_tests/paper_trade_live/test_plan_only_live.py`), not from the CLI. This was
chosen over a `money-pit plan` command because the tier already carries exactly the gating the
capability wants: deselected by default via `addopts = "-m 'not live and not live_video'"`,
additionally skipped unless `MONEY_PIT_LIVE=1`, and guarded by `assert live_credentials.paper`. A
CLI command would be a new always-present surface needing its own guard to stay operator-only.

That tier writes to the real `DAILY_SHOW_ROOT / slug` rather than a `tmp_path`, deliberately: the
artifacts exist to be read after the run, alongside the operator's other daily-show runs, not
garbage-collected by pytest.

Alpaca credentials remain a single global paper/live flag (ADR 0013), unchanged and not in scope
here. Plan-only runs execute against the paper account like every other run.

### Emails are not suppressed

Owner mail is reachable before execution by two distinct routes, and a plan-only run sends both
normally. The mode is a fidelity instrument; suppressing a production side effect would mean
verifying something other than what production does. The `with_undelivered_record` wrapper remains
available if this proves noisy in practice.

- The **notification node**, on the `ANALYSIS_HALT` and `VALIDATION_ERROR` paths.
- The **recovery node**, which sends directly rather than routing through the notification node
  (`pipeline/recovery.py:206-226`), on `HALT` and `PROCEED_WITH_NOTICE`.

The recovery route has a consequence worth stating plainly, because it is the one way a plan-only
run produces no plan at all: recovery is the *entry* node, and a prior run with a leg still open at
the broker halts this run before any planning happens. Since the live tier writes to the real
`DAILY_SHOW_ROOT` alongside real prior runs, that is genuinely reachable — an operator who ran a
full paper trade earlier can get a "Recovery Halt" email and an empty run directory instead of the
plan they were after. That is correct behavior, not a defect: a still-open prior leg is exactly the
condition under which no new plan should be formed. But it means a plan-only run halting early is a
result to read, not a malfunction, and it is why the tests pin that a plan was actually produced
rather than only that no order was placed.

### Consequences

- Good, because the operator can see the full planned action set — `action_steps.json`,
  `action_steps_validation.json`, `validation_status.json` and every upstream artifact — without any
  order being placed.
- Good, because production is provably unaffected: the `False` path compiles exactly as before, and
  the offline tier runs both paths over identical signals and overrides, differing only in the flag.
- Good, because composition of the capital-critical dependencies still happens. `production_deps`
  resolves Alpaca credentials and wires `place_order` eagerly even in plan-only mode, so a
  verification run also proves the write path *composes* — it simply never invokes it.
- Bad, because the returned in-memory state still carries `determination == PROCEED` and
  `sub_agent_spawned == "execution"`. This is correct for a mid-flight pause, but it is a trap for
  anyone asserting on state rather than on artifacts. The tests assert on file absence for this
  reason.
- Bad, because the graph now has two compile paths, and a future node inserted between determination
  and execution would fall on the planning side of the pause by default. The guard is that the
  plan-only expected-step list is an **independent literal** (`_PLAN_ONLY_PATH_STEPS`), not derived
  from the execute-path list: adding a node forces someone to update both and thereby to decide
  consciously which side of the pause it belongs on. An earlier draft derived one list from the
  other, which silently absorbed any such node and caught nothing.
- Neutral, because the `InMemorySaver` is discarded with the process. Nothing is resumable, by
  design — see below.

### Confirmation

`tests/integration_tests/pipeline/test_paper_trade_e2e.py` runs both paths over identical signals
and overrides, differing only in the flag. The plan-only pins assert that `completed_steps` equals
`_PLAN_ONLY_PATH_STEPS` exactly — so a leaked execution node fails rather than passing a membership
check — that neither `execution_journal.json` nor `determination.json` exists, and that the planning
artifacts are present and schema-valid. The pre-existing execute-path assertions are the contrast
that proves the flag gates something rather than the artifacts merely never being produced in that
tier.

`tests/integration_tests/paper_trade_live/test_plan_only_live.py` exercises the real path — real A1
LLM, real paper-account portfolio fetch, real retrieval — and asserts no execution journal was
written. It locates the run directory from the returned state's `working_dir` rather than
recomputing the minted slug.

Both tiers pin that a plan was *positively produced*, not merely that no order was placed. Absence
of an execution journal is satisfied by any early stop — a recovery halt, `NO_ACTION`, or
`ANALYSIS_HALT` — and would even be satisfied by a full production run that happened to end in
`NO_ACTION`. Without a positive pin the live test would not discriminate plan-only mode at all.

## Pros and Cons of the Options

### Pause with a LangGraph interrupt

- Good, because "paused" is what is actually happening; no vocabulary has to be invented for it.
- Good, because the graph topology is identical between modes — only the compile differs — so the
  two cannot structurally drift.
- Bad, because it requires a checkpointer and a thread id, machinery the pipeline otherwise has no
  use for.

### Reroute the `EXECUTE` branch to the finalizer

- Good, because it needs no checkpointer and no new dependency surface.
- Bad, because the finalizer would write a `determination.json` asserting `sub_agent_spawned:
  execution` with outcome `none` — a persisted lie in the capital audit trail. Decisive against.
- Bad, because it creates a second graph topology that can drift from production as nodes are added.

### Inject a refusing `place_order`

- Good, because `PipelineOverrides.place_order` is an existing seam; no graph change at all.
- Bad, because the execution node still runs and writes an execution journal recording failures that
  did not happen. It replaces one dishonest artifact with a worse one, and would land the run in
  `EXECUTION_FAILED` — indistinguishable from a genuine outage.

### Add a `TerminalState` for planned-not-executed

- Good, because it would make the stop explicit in the state machine.
- Bad, because the enum models *conclusions*, and a deliberately-stopped run has none. Every
  consumer would need a branch for a value that means "ignore this run."

## More Information

The paper/live credential flag is governed by [ADR 0013](0013-explicit-alpaca-paper-flag.md) and is
untouched: plan-only runs use the same paper credentials as every other run. Execution journal
semantics are [ADR 0003](0003-execution-journal-semantics-paper-stub.md); the absence of a journal
is the capital-critical assertion here.

Deliberately **not** built: resuming a paused run into a real execution later. That would require a
durable checkpointer surviving the process and a command that moves capital on a previously-computed
plan — the sharpest edge in the repo, and a decision worth taking on its own merits rather than
inheriting from this one. Nothing here forecloses it; the interrupt mechanism is the same one a
resumable design would build on, but the `InMemorySaver` would have to become durable and the
operator-facing surface would need its own guard. If that is ever wanted, it gets its own ADR.
