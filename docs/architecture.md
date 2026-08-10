# Architecture

money-pit 0.0.2 is one persistent staged system. SQLite owns cross-run intelligence and immutable files own acquired assets, run artifacts, and reports. Every run has a UUID and an explicit `as_of` cutoff.

## Stages

1. A1 interprets pending durable evidence into immutable claim observations.
2. A2 discovers candidate theses from holdings, watchlists, benchmark constituents, source mentions, exposure gaps, configured screens, and explicit proxies.
3. A3 executes bounded read-only research and admits fetched material as durable evidence before citation.
4. A4 resolves and verifies claims, applies temporal compatibility, and appends complete thesis revisions.
5. A5 applies evidence eligibility, deterministic return calibration, liquidity and risk constraints, and produces an exact decision snapshot and portfolio plan.
6. A6 revalidates the exact plan immediately before submitting orders through the sole write gateway.

Agents receive typed outputs and only the capabilities required by their stage. A1–A5 cannot construct a broker writer. Deterministic code owns identity, freshness, temporal admission, capital eligibility, optimization, hashes, approvals, and execution controls.

SecretSpec resolves credentials at the outer capability boundary. The application converts each allowlisted result into a frozen typed credential and closes the SDK result. It does not export credentials into process state. Paper and live portfolio and execution scopes are separate. Replay constructs no credential resolver.

## Persistence and replay

The database starts from the single 0.0.2 schema. Initialization rejects a non-empty database with an unknown fingerprint. Claims, verifications, thesis revisions, decisions, plans, approvals, executions, and outcomes are append-only. Point-in-time reads filter by `known_at` and the requested cutoff; replay cannot observe later state and never executes A6.

## Capital authority

Only supported US equities and ETFs can gain exposure. Mixed, unresolved, contradicted, expired, unauthorized, illiquid, untradable, or incompletely covered evidence cannot create exposure. Bearish theses may reduce long holdings but cannot create shorts. Live execution defaults to approval-required authority covering one exact, unexpired plan hash.
