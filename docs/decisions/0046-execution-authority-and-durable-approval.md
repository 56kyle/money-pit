# Explicit execution authority and durable plan approval

## Context and problem statement

The original pipeline coupled paper/live broker routing to a boolean and eagerly created
an order writer for every production run. A validated analysis could therefore flow directly
to an order without an operator decision bound to immutable portfolio state. The persistent
portfolio platform needs separate broker-environment and execution-authority decisions,
tamper-evident approvals, an emergency stop, and replay-safe submissions.

## Decision drivers

- Observation and planning must not create a broker write client.
- Approval must cover one exact canonical plan hash and expire with that plan.
- Missing policy, state, approval, or control data must stop execution.
- A crash before or after submission must leave durable recovery evidence.
- Existing paper configuration and historical journals must remain readable during migration.

## Considered options

### Keep the run pipeline's automatic execution edge

Rejected. It conflates analytical validation with capital-write authority and cannot safely
combine evidence across persistent plans.

### Store approvals beside plan JSON files

Rejected as the authority. File discovery has weak concurrency and uniqueness semantics.

### Use SQLite authority records and a lazy write-client boundary

Accepted. SQLite transactions provide immutable decisions, a singleton kill switch, and
unique per-trade execution claims. The broker writer is created only after explicit execution
revalidates current state.

## Decision outcome

`BrokerEnvironment` and `ExecutionMode` are separate enums. `APPROVAL_REQUIRED` is the default.
Legacy analysis commands route to finalization instead of execution unless autonomy is explicitly
configured. Explicit plan execution must load a validated `PortfolioPlan`, compare its canonical
hash, check expiry and all mandatory evidence and constraint gates, load the latest exact-hash
decision, check the global kill switch, refresh portfolio and market state, and obtain a unique
trade claim before submission. These checks repeat immediately before every leg. Stable client
order IDs derive from the plan hash and trade index.

The database seeds execution as disabled. An explicit operator may enable the global switch with
an actor and reason, but that switch grants no authority to any plan and bypasses no other gate.
Autonomous execution fails closed until a versioned coverage evaluator exists for every proposed
action.

The target guarded executor journals intent and claims a trade before submission. In this milestone
that gateway is not connected to a broker writer, so explicit plan execution stops safely after
authorization prerequisites. Existing journals remain valid because plan identity fields are
optional when reading legacy records. Recovery retains its legacy directory scan until every
execution path records queryable database claims. The ledger records both compatibility paths.

## Consequences

- Approval-required plans can be reviewed and decided without opening a write transport.
- Rejections, approvals, and execution claims are durable and auditable.
- Operators must configure a complete versioned execution policy before explicit execution.
- The legacy `MONEY_PIT__ALPACA_PAPER` setting remains a migration input only; the runtime credential
  boundary exposes `BrokerEnvironment`.

## Validation

Tests must pin exact-hash approval, rejection precedence, missing control state, kill-switch changes
between legs, plan expiry, failed or missing gates, unknown-tax autonomous sells, drift, duplicate
claims, stable client IDs, intent-before-submit ordering, and the absence of write-client creation
during ordinary planning commands.
