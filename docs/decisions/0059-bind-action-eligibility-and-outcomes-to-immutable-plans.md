# Bind action eligibility and outcomes to immutable plans

## Status

Accepted

## Context and Problem Statement

Evidence that is sufficient to retain or reduce a holding is not necessarily sufficient to create new exposure. Outcome measurements are also meaningless when they are detached from the thesis revision and portfolio plan that produced them.

## Decision Drivers

- Prevent unresolved, contradicted, expired, mixed, or unauthorized evidence from creating exposure.
- Preserve bearish reductions without granting short authority.
- Measure recommendations without automatic policy or prompt mutation.

## Considered Options

- Use one binary eligibility gate for every action.
- Average signals into a mutable score.
- Use action-tiered eligibility and immutable, version-bound outcome schedules.

## Decision Outcome

Apply deterministic action tiers before optimization. New exposure requires complete authoritative evidence, market, risk, liquidity, and tradability coverage. Held positions may be retained or reduced under narrower authority, but 0.0.2 remains long-only. After plan persistence, schedule event, review, and horizon observations bound to exact thesis revision IDs, plan ID/hash, and benchmark snapshot.

Supporting and contradicting claim keys remain separate inputs. Any material contradiction denies
new exposure unless that contradiction is itself currently and authoritatively disproved; mixed,
unresolved, stale, or unauthorized contradiction evidence is not harmless. Outcome schedules bind
the exact scenario distribution, prompt and model versions, portfolio provider, and the configured
benchmark identity, provider, constituent weights, composition hash, baseline snapshot hashes, and
content-hashed future observation queries. Evaluation accepts only
content-addressed timestamped observation series matching those bindings; returns, drawdown, and
the nearest realized scenario are derived over the exact baseline-to-evaluation interval rather
than accepted as caller attribution. Static report bundles are
installed atomically and include durable evidence links or explicit locator-unavailable reasons,
claim-category source-authority ratios, scenarios, conflicts, decision diagnostics, covariance component risk,
and true target-minus-current return and variance changes.

### Consequences

- Every buy has stricter provenance than a hold or reduction.
- Incompatible signals remain separately visible.
- Reports and metrics remain audit artifacts and cannot self-train agents or mutate policy.

## Validation

Eligibility, optimizer, expired-evidence, contradiction, tax-lot, plan-hash, outcome-schedule, and replay tests pin the behavior.
