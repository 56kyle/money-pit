# A6 owns the only capital-write boundary

## Status

Accepted

## Context and problem statement

Investment research, replay, and portfolio planning need broker reads, but they do not need
authority to move capital. Constructing a write client during those stages makes an analytical
run capable of submitting an order accidentally. Approval also has little value unless it binds
the exact immutable plan and execution rechecks the state that justified that plan.

The execution subsystem must survive ambiguous broker responses and process interruption without
submitting a duplicate order. Autonomous execution is a later authority level, not a synonym for
running the harness unattended.

## Decision drivers

- No stage before A6 may construct or receive a broker writer.
- Replay must be structurally unable to write.
- Operator approval must cover one exact, unexpired plan hash.
- State drift and unavailable state must stop execution.
- An intent must be durable before submission, and every later phase must be append-only.
- A retry must reconcile uncertain prior submissions instead of replaying them.
- Autonomous authority must require explicit policy and operational evidence.

## Considered options

### Give the harness a broker client and rely on routing

Rejected. A routing defect or replay composition error would expose a write capability outside
the authority boundary.

### Approve an instrument list or a run identifier

Rejected. Neither covers the exact weights, quantities, evidence, constraints, portfolio state,
market state, tax inputs, and policy used to create a plan.

### Retry failed submissions directly

Rejected. A timeout can occur after the broker accepted the order. Direct retry can duplicate
exposure.

### Use one lazy A6 gateway with durable claims and reconciliation

Accepted. A6 first validates authority and current state, then constructs the environment-specific
writer. It revalidates immediately before each leg, records intent, atomically claims the stable
client order identity, submits, and appends each observed transition. Any uncertain claim blocks
new execution until read-only broker reconciliation proves it terminal.

## Decision outcome

`BrokerEnvironment` is `PAPER` or `LIVE`. `ExecutionMode` is `OBSERVE`,
`APPROVAL_REQUIRED`, or `AUTONOMOUS`. Live execution defaults to approval-required. Enabling the
global kill switch requires an actor, reason, explicit policy configuration, and confirmation; it
does not approve a plan.

Approval-required execution accepts only the latest decision for the exact plan hash. The approval
also binds the active execution configuration hash, complete execution-policy fingerprint and
version, broker environment, broker account, and already committed daily turnover. A change to
any policy or account binding invalidates the approval. A5 plans without this execution binding
remain observable but cannot execute until approval records the exact current authority.

Before the
writer is constructed, A6 atomically reserves cumulative turnover by account and America/New_York
trading date. Settled reservations remain committed, released reservations restore capacity, and
an ambiguous broker response retains its reservation. Before that reservation and again before
every trade, A6 checks the kill switch, plan integrity and
expiry, policy version, evidence and constraint gates, order notional, cumulative same-day
turnover, each trade's authoritative asset class, tax coverage, portfolio positions, account
status and trading block, cash, open orders, timestamped market freshness, market drift, and
evidence freshness. A missing
or unavailable check is a denial. After a confirmed fill, the next check compares reality with the
deterministic expected post-fill positions and cash, not with the original pre-trade portfolio.

The database stores immutable execution events and the latest monotonic state of each idempotent
trade claim. Stable client order identifiers derive from the plan hash and trade index. Submission
uncertainty leaves the claim nonterminal. Recovery queries those claims and observes the broker by
client order identifier. It never blind-replays or automatically unwinds an order.

An exact operator rejection is an unconditional veto in all modes. Autonomous execution may bypass
only the absence of an approval and additionally requires an explicitly enabled policy with the same version,
60 shadow trading days, 30 executable shadow plans, 30 approved paper executions, 30
approval-required live executions, and zero critical control failures in the validation period.
Its initial scope is liquid, long-only US equities and ETFs. Every instrument needs explicit current
asset-class and liquidity coverage, and no trade may open short exposure. Aggregate qualification
counts are accepted only when a durable evidence verifier confirms their dated source-event IDs.
Autonomous order-notional and turnover caps must be stricter than the base execution policy.

## Consequences

### Positive

- Analytical and replay code cannot reach capital writes through ordinary dependency composition.
- Approval, intent, submission, fill, and recovery evidence are durable and attributable.
- A crash or ambiguous response fails closed without making a duplicate submission the recovery
  strategy.
- Autonomous authority has measurable prerequisites and an auditable opt-in.

### Negative

- Execution requires more current-state providers than planning.
- One stale or unavailable provider pauses the entire remaining plan.
- Operators must reconcile uncertain orders before any later plan can execute.

## Validation

Tests pin writer construction after preflight only, exact-hash and exact-policy approval,
environment and account mismatch, per-trade equity and ETF authority, cumulative turnover,
blocked accounts, stale quotes, repeated per-leg
revalidation, kill-switch changes between legs, all drift and freshness denials, intent-before-send,
idempotent claims, submission uncertainty, terminal and nonterminal recovery, autonomy thresholds,
policy and scope coverage, and the absence of a writer from replay and A1-A5 composition.
