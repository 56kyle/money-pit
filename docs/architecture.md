# Architecture

money-pit 0.0.4 is a persistent staged system. SQLite owns cross-run intelligence and immutable files own acquired assets, run artifacts, and reports. Every update has a UUID and an explicit point-in-time boundary.

## Stages

1. A1 interprets pending source-item bundles into immutable claim observations.
2. A2 consumes compact, durable discovery units and creates candidate theses.
3. A3 resumes bounded candidate research jobs and admits fetched material as verification evidence.
4. A4 resolves terminal research jobs and appends complete thesis revisions.
5. A5 applies evidence eligibility, deterministic return calibration, liquidity and risk constraints, and produces an exact decision snapshot and portfolio plan.
6. A6 revalidates the exact plan immediately before submitting orders through the sole write gateway.

Each A1–A4 model call advances one durable work unit. Source-specific updates follow work caused by one source. Global updates select a bounded, deterministic batch. Successful work remains complete when a later call fails. Candidate research resumes by material-premise fingerprint and stops after sufficient independent evidence, a decisive contradiction, or no useful provenance.

Agents receive typed outputs and only the capabilities required by their stage. A1–A5 cannot construct a broker writer. Deterministic code owns identity, freshness, temporal admission, work selection, capital eligibility, optimization, hashes, approvals, and execution controls. The inference ledger stores token counts and request hashes, but never prompt text or calculated cost.

SecretSpec resolves credentials at the outer capability boundary. Each credential is required within its scope, not across the complete manifest. Disabled providers and adapters do not request their scopes. If the application invokes an enabled capability, a missing or blank credential causes a typed error that does not include credential values. The application converts each allowlisted result into a frozen typed credential and closes the SDK result. It does not export credentials into process state. Paper and live portfolio and execution scopes are separate. Replay constructs no credential resolver.

## Persistence and replay

The database starts from the 0.0.4 schema. Initialization atomically migrates only an exact 0.0.3 predecessor. The migration preserves existing evidence, interpretations, candidates, research sessions, and run artifacts, then backfills durable incremental work identities. Unknown releases, metadata drift, and catalog drift are rejected before mutation. Claims, verifications, thesis revisions, decisions, plans, approvals, executions, and outcomes are append-only. Point-in-time reads filter by `known_at` and the requested cutoff; replay cannot observe later state and never executes A6.

Intelligence status and run inspection read only SQLite state. They do not load capability credentials or construct providers. Portfolio review composes A5 independently from the incremental intelligence runner and uses the latest eligible durable thesis state.

## Capital authority

Only supported US equities and ETFs can gain exposure. Mixed, unresolved, contradicted, expired, unauthorized, illiquid, untradable, or incompletely covered evidence cannot create exposure. Bearish theses may reduce long holdings but cannot create shorts. Live execution defaults to approval-required authority covering one exact, unexpired plan hash.
