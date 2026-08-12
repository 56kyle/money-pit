---
status: accepted
date: 2026-08-12
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Allocate agent research plans within durable budgets

## Context and Problem Statement

The A3 planner receives the remaining query and fetch budgets, but its typed output can still request
more aggregate results than remain. The durable runner correctly rejects such a plan before provider
I/O. Treating that rejection as a terminal run failure, however, lets advisory model arithmetic stop
otherwise valid bounded research.

## Decision Drivers

- Never let model output expand deterministic query or fetch authority.
- Preserve coverage across distinct admitted research questions.
- Keep task order and the original planner response auditable.
- Retain the runner's independent pre-I/O and post-I/O budget checks.

## Considered Options

- Reject the complete plan when either aggregate budget is exceeded.
- Admit only whole tasks until the next requested result cap does not fit.
- Allocate at least one result to each admitted task, then distribute remaining capacity in task order.

## Decision Outcome

Before each round, orchestration admits no more tasks than the remaining query count or fetch count.
It reserves one result for every admitted task, then distributes the remaining fetch capacity in
planner order up to each task's requested `maximum_results`. Tasks beyond the admitted query count
remain unexecuted. The immutable planner response retains the original requested caps, while the
durable planned-task record retains the same original task identity and requested cap. Its linked
execution task contains the deterministic allocated cap. Completing the execution therefore also
completes the original plan; allocation never creates a second planned-task identity or leaves the
original pending for retry.

The durable runner continues to reject any allocation that exceeds its assigned budgets before I/O
and continues to compare actual query and fetch consumption with those budgets after execution.
The planner prompt also states the aggregate arithmetic explicitly, reducing avoidable corrections
without treating prompt compliance as authority.

### Consequences

- Good, because one oversized task cannot starve every later admitted research question.
- Good, because allocated query and fetch authority is deterministic and locally testable.
- Good, because the harness can only reduce model-requested work.
- Bad, because a late task may receive fewer results than the planner preferred.
- Neutral, because another round may revisit unresolved material premises when budget remains.

## Pros and Cons of the Options

### Reject the complete plan

- Good, because the runner remains simple.
- Bad, because recoverable planner arithmetic becomes a terminal workflow failure.

### Admit only whole tasks

- Good, because every executed task retains its requested result cap.
- Bad, because an early large task can waste remaining capacity or starve later independent questions.

### Reserve one result per admitted task

- Good, because it preserves breadth before adding depth.
- Good, because the sum of allocated caps exactly respects the remaining fetch budget.
- Bad, because ordered surplus allocation favors earlier tasks.

## Confirmation

Tests pin the reported `(5, 5, 5, 5, 20)` request against 19 remaining fetches as
`(5, 5, 5, 3, 1)`, query-count truncation, later-task non-starvation, and exhausted-budget behavior.
The persistence test also proves that requested caps remain auditable, allocated caps reach the
execution tasks, and no original plan remains pending.
The full unit and integration suite, Ruff, basedpyright, and `git diff --check` must pass.
