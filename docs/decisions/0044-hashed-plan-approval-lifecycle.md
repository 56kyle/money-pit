---
status: accepted
date: 2026-07-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Bind Approval and Execution Authority to an Exact Hashed Portfolio Plan

## Context and Problem Statement

A portfolio plan can become unsafe after review without its proposed trades visibly changing.
Positions, market data, evidence gates, constraints, policy, or plan expiry may change. Approval of
a mutable plan identifier alone would authorize content the operator never reviewed.

Execution authority also has three distinct meanings: observation, explicit approval, and bounded
autonomy. Broker environment is independent of that authority. The lifecycle must fail closed
without representing these states as interacting booleans.

## Decision Drivers

- Approval must cover exactly the reviewed payload.
- Rejections and approvals must be immutable audit records, not mutable plan fields.
- Any portfolio, market, policy, evidence, or constraint drift must invalidate authorization.
- Expired plans and a global execution disable must stop every execution mode.
- Observation mode must be structurally unable to authorize execution.
- Autonomous execution must reject tax-unknown sells.
- Report output must never act as authorization input.

## Considered Options

- Hash the canonical plan payload and bind decisions to the digest
- Approve a mutable plan row by identifier
- Copy an `approved` boolean onto the plan

## Decision Outcome

Hash the entire `PortfolioPlanPayload` with SHA-256 using its sole canonical serialization method.
The digest covers snapshot identifiers, policy, model and prompt versions, targets, trades,
rejections, estimates, and gate results. `recompute_plan_hash` deliberately delegates to that
canonical payload representation so a second serialization convention cannot emerge.

An `ApprovalRecord` or `RejectionRecord` contains a decision identifier, exact plan identifier and
digest, actor, and aware timestamp. Decisions are frozen values and never mutate the plan.

Immediately before execution, `authorize_plan`:

1. recomputes and compares the plan digest;
2. checks the global execution enable;
3. rejects observation mode;
4. requires the trusted evaluation time to be strictly before expiry;
5. compares portfolio snapshot, market snapshot, policy version, evidence gates, and deterministic
   constraint results with the plan-bound values;
6. requires all evidence and constraint gates to be present and passing;
7. in approval-required mode, requires an approval matching the exact plan identifier and digest;
8. in autonomous mode, rejects a supplied rejection and every sell with unknown tax cost; and
9. returns an immutable `ExecutionAuthorization` bound to the same plan and current state.

Snapshot identifiers are content fingerprints, not arbitrary row identifiers. Portfolio
fingerprints must cover account identity, cash, positions, quantities, and relevant prices; market
fingerprints must cover every price and market datum used by the plan. The snapshot-producing
service owns canonical fingerprint construction. Until those producers supply content
fingerprints, authorization must not be treated as proof against state drift.

Execution must consume the returned authorization immediately and compare its plan and state
identifiers again at the order-submission boundary. This narrows, but cannot eliminate, the race
between observation and broker submission.

### Consequences

- Good, because changing any covered plan field invalidates prior approval.
- Good, because decisions form an append-only audit trail.
- Good, because approval-required and autonomous authority share the same deterministic gates.
- Good, because a report renderer cannot grant authority.
- Bad, because every material state change requires a new plan and approval.
- Bad, because content fingerprint quality is now a security property of snapshot producers.
- Neutral, because a valid authorization is short-lived evidence, not an executed-order record;
  the existing execution journal remains authoritative for submission and fills.

### Confirmation

Tests must cover payload tampering, plan expiry at the exact boundary, missing and mismatched
approval, rejection, every state-drift field, missing and failed gates, observe mode, kill-switch
behavior, autonomous tax-unknown sells, repeated authorization, and immutable records. A
failure-injection integration test must change broker-observed state between authorization and
submission and verify that the execution boundary stops.

## Pros and Cons of the Options

### Hash the canonical plan payload

- Good, because approval is content-addressed and independently recomputable.
- Good, because immutable decisions remain useful after the plan expires.
- Bad, because canonicalization becomes a long-lived compatibility contract.

### Approve a mutable plan row by identifier

- Good, because the database workflow is simple.
- Bad, because edits after review retain authority. This is decisive against.

### Copy an `approved` boolean onto the plan

- Good, because querying current state is easy.
- Bad, because it loses decision history and actor identity.
- Bad, because it conflates plan content, operator intent, and execution state.

## More Information

Broker environment remains modeled separately by `BrokerEnvironment`; a live account never implies
autonomous authority, and a paper account never weakens authorization checks.
