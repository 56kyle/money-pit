---
status: accepted
date: 2026-08-09
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Let evidence drive portfolio targets

## Context and Problem Statement

The persistent research workflow must discover and test investment theses rather than converge on a
preselected institutional allocation. Mandatory core targets and a residual satellite bucket made
configured membership an allocation instruction. They also made an existing policy breach
infeasible, which could force a liquidation instead of a bounded reduction.

Model and source credentials were also resolved through ambient provider conventions. That made
namespaced configuration unreliable and could require credentials before the related capability was
used.

## Decision Drivers

- Broker holdings and cash are the only initial capital state.
- Research membership must not grant capital authority.
- New exposure requires a fully eligible thesis and complete deterministic market inputs.
- Existing breaches must not grow, but one review must not forcibly remove them.
- Research references must not acquire capital authority from model-authored ticker text.
- A decision snapshot must retain the complete observed market state even when optimization uses a subset.
- Credential use must be explicit, namespaced, and capability-scoped.

## Considered Options

- Keep fixed core targets and tune their weights.
- Treat all configured instruments as optimizer candidates with zero weights when ineligible.
- Separate research and optimization universes and apply grandfathered exposure limits.

## Decision Outcome

Remove core and satellite allocation concepts. Strategy configuration classifies every configured
capital instrument as a single stock, broad-market equity ETF, thematic equity ETF, fixed-income
ETF, or cash-equivalent ETF. Per-position limits use the minimum of the default, any instrument
override, and current liquidity capacity. Independent aggregate limits control equity, single-stock,
thematic, sector, factor, and configured correlated exposure.

The optimizer receives current holdings plus candidates that pass every new-exposure gate. The
larger layered universe remains available to discovery and reporting. Benchmark membership,
watchlist membership, and proxy status never grant optimizer admission.

For every breached unsigned upper bound, the effective limit is the greater of the configured limit
and the current exposure. The target may not increase the breach. A signed factor with configured
limit `L` and current exposure `c` instead uses the exact bounds `min(-L, c)` and `max(L, c)`.
This grandfathers only the breached side and does not create equal authority on the opposite side.
The solver and deterministic result validator use the same bound helper; numerical tolerance is
applied only when validating a returned result. Turnover, cash, and position-change constraints
remain binding.

Candidate discovery separates the optional model-authored `instrument_reference` used for research
from the optional, deterministic `instrument` that carries capital authority. Theme-only hypotheses
may leave both fields null. An exact configured universe reference may resolve both fields. An
authorized observation-only or source-grounded reference persists with `instrument = null`, remains
researchable, and cannot be promoted or enter A5 until deterministic resolution exists. A genuine
theme-only candidate with both fields null may become an instrument-free thesis. A4 may only
preserve the resolved instrument from a candidate or prior revision; it cannot infer a ticker or
proxy.

Market snapshots retain every quote captured for the decision. Optimization instruments must be a
subset of those quotes, optimizer results must exactly cover the optimization instruments, and
trade metadata must cover every target. Trade derivation selects only target quotes and ignores the
remaining snapshot quotes. This preserves replay evidence without making unrelated quotes optimizer
inputs.

OpenAI agents and vision processing use one provider factory backed only by
`MONEY_PIT__OPENAI_API_KEY`. YouTube connectors receive `MONEY_PIT__YOUTUBE_API_KEY` from a
configuration-bound adapter closure. Credential-dependent sources remain constructible only when
their capability is invoked.

### Consequences

- Good, because research can change the portfolio without a predetermined target allocation.
- Good, because ineligible ideas remain inspectable without entering optimization.
- Good, because existing concentrated positions can be reduced over multiple bounded reviews.
- Good, because unresolved research subjects remain durable without silently becoming tradable.
- Good, because replay retains the complete market observation while planning remains subset-scoped.
- Good, because provider secrets cannot fall back to unrelated ambient variable names.
- Bad, because every capital-eligible instrument needs explicit risk and exposure metadata.
- Neutral, because VTI remains an evaluation benchmark and has no target-weight authority.

## Pros and Cons of the Options

### Fixed core targets

- Good, because feasibility and diversification are simple to express.
- Bad, because configuration predetermines the destination before thesis research supports it.

### One inclusive optimizer universe

- Good, because every candidate has one uniform vector representation.
- Bad, because mere optimizer admission can obscure the distinction between research and capital
  authority.

### Separate universes with grandfathered limits

- Good, because portfolio authority is explicit and fail-closed.
- Good, because current policy breaches cannot increase or require immediate liquidation.
- Bad, because point-in-time eligibility and exclusion reasons must be preserved alongside the
  smaller optimizer input.

## More Information

This decision refines the deterministic optimizer in [ADR 0043](0043-cvxpy-portfolio-optimizer.md)
and configuration responsibility boundaries in
[ADR 0058](0058-load-configuration-by-stage-responsibility.md).
