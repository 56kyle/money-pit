# Architecture

money-pit 0.0.6 is a persistent staged system. SQLite owns cross-run intelligence and immutable files own acquired assets, run artifacts, and reports. Every update has a UUID and an explicit point-in-time boundary.

## Stages

1. A1 interprets pending source-item bundles into immutable claim observations.
2. A2 creates immutable proposals and exact semantic variants. Equal variants join one canonical hypothesis group; ambiguous variants require review.
3. A3 resumes one versioned research case for each canonical group and material premise. Job-scoped tasks preserve their discovery origins and reused executions.
4. A4 runs only after a qualifying evidence or decisive contradiction assessment. It resolves candidate-grounded claim semantics, and its identity excludes operational history.
5. A5 applies evidence eligibility, deterministic return calibration, liquidity and risk constraints, and produces an exact decision snapshot and portfolio plan.
6. A6 revalidates the exact plan immediately before submitting orders through the sole write gateway.

Each A1–A4 model call advances one durable work unit. Source-specific updates follow work caused by one source. Global updates select a bounded, deterministic batch. Successful work remains complete when a later call fails. Candidate research resumes by material-premise fingerprint and stops after sufficient independent evidence, a decisive contradiction, or no useful provenance.

Agents receive typed outputs and only the capabilities required by their stage. A1–A5 cannot construct a broker writer. Deterministic code owns identity, freshness, temporal admission, work selection, capital eligibility, optimization, hashes, approvals, and execution controls. The inference ledger stores token counts and request hashes, but never prompt text or calculated cost.

Candidate wording and provenance do not define semantic identity. An exact variant uses normalized capital reference, direction, horizon, theme, causal mechanisms, and regime assumptions. Each variant starts in a separate canonical group. Equal variants link automatically. An ambiguous match requires an append-only operator resolution and makes only its related work unavailable. Research that stops without qualifying evidence records `insufficient_evidence` and does not invoke A4.

SecretSpec resolves credentials at the outer capability boundary. Each credential is required within its scope, not across the complete manifest. Disabled providers and adapters do not request their scopes. If the application invokes an enabled capability, a missing or blank credential causes a typed error that does not include credential values. The application converts each allowlisted result into a frozen typed credential and closes the SDK result. It does not export credentials into process state. Paper and live portfolio and execution scopes are separate. Replay constructs no credential resolver.

## Persistence and replay

The database starts from the 0.0.6 schema. Initialization atomically migrates only an exact 0.0.5 predecessor. The migration preserves existing evidence, interpretations, candidate proposals, research sessions, waves, usage, and run artifacts. It adds semantic variants, canonical groups, append-only reviews and resolutions, job-scoped task lineage, eligibility assessments, and explicit supersession. Unknown releases, metadata drift, and catalog drift are rejected before mutation. Claims, verifications, thesis revisions, decisions, plans, approvals, executions, outcomes, and operator resolutions are append-only. Point-in-time reads filter by `known_at` and the requested cutoff; replay cannot observe later state and never executes A6.

Intelligence status, audit, review listing, lineage, and run inspection read only SQLite state. They do not load capability credentials or construct providers. Portfolio review composes A5 independently from the incremental intelligence runner and uses the latest eligible durable thesis state.

## Capital authority

Only supported US equities and ETFs can gain exposure. Mixed, unresolved, contradicted, expired, unauthorized, illiquid, untradable, or incompletely covered evidence cannot create exposure. Bearish theses may reduce long holdings but cannot create shorts. Live execution defaults to approval-required authority covering one exact, unexpired plan hash.
