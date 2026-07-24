---
status: accepted
date: 2026-06-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# v0 Regime Classification Uses Scalar Indicator Readings with Structural Thresholds

## Context and Problem Statement

`design_decisions.md §1` specifies a `discretize(series: list[float], ...)` function that computes z-scores from a trailing window of readings. `pipeline_contracts.md §8a` defines `MacroIndicators` with a single `float | None` per indicator — the current reading that the A3 retrieval step fetches from FRED. These two designs are incompatible: FRED retrieval returns one scalar, not a time series, and `MacroIndicators` carries no history.

How should the v0 regime classifier discretize a scalar reading into a signal (-1, 0, +1) without a trailing window?

## Decision Drivers

- `MacroIndicators` is a single-reading snapshot; there is no trailing series available at classification time.
- Each of the five macro indicators has a well-established, structurally meaningful neutrality boundary (not an arbitrary hyperparameter) that is domain-stable across market cycles.
- `regime_lookback` and the z-score approach remain viable for v1 once A3 is extended to retrieve or accumulate historical observations.

## Considered Options

- Structural thresholds
- Z-score from trailing window
- Percentile rank

## Decision Outcome

Chosen option: "Structural thresholds", because it is the only approach compatible with the current `MacroIndicators` contract (a single scalar per indicator), and the threshold values are structurally meaningful rather than arbitrary.

The five structural thresholds:

| Indicator                              | Threshold   | Rationale                          |
| -------------------------------------- | ----------- | ---------------------------------- |
| `yield_curve` (T10Y2Y)                 | 0.0 pct pts | Inversion boundary                 |
| `credit_spreads` (HY OAS)              | 3.0 pct pts | Long-run neutral for HY spreads    |
| `pmi` (regional Fed mfg diffusion composite) | 0.0   | Net-diffusion expansion/contraction boundary (see ADR 0033) |
| `earnings_revisions` (fwd-EPS breadth) | 0.0         | Zero breadth = flat revisions      |
| `inflation` (CPILFESL YoY)             | 2.5%        | Long-run Fed target                |

Orientations are structural (not configurable): `yield_curve`, `pmi`, `earnings_revisions` are higher-is-better; `credit_spreads` and `inflation` are lower-is-better. `regime_band` (default 0.5) defines the neutral zone half-width.

### Consequences

- Good, because no historical series is required at classification time — the function is pure and stateless.
- Good, because structural thresholds are domain-interpretable and do not drift with market regimes the way z-scores do.
- Bad, because absolute levels are more regime-sensitive than z-scores; a PMI of 49.5 may be meaningfully different in 1982 versus 2024.
- Bad, because `regime_lookback` is unused in v0; the `RECOVERY` rule (which needs prior-period state comparison) is deferred.
- Neutral, because `RECOVERY` resolves to `GROWTH_ACCELERATING` in v0 when all growth signals are strong — an acceptable approximation for the early pipeline.

### Confirmation

`tests/unit_tests/compute/test_regime.py` exercises all reachable `RegimeTag` values and asserts `RECOVERY` is not reachable across all 3^5 = 243 discretized signal combinations.

## Pros and Cons of the Options

### Structural thresholds

- Good, because compatible with the current `MacroIndicators` single-scalar contract.
- Good, because thresholds encode genuine economic meaning (ISM 50, yield-curve inversion) that a reviewer can validate without data.
- Bad, because absolute levels do not adapt to secular shifts in rates, spreads, or inflation regimes.

### Z-score from trailing window

- Good, because z-scores are regime-adaptive and reduce sensitivity to secular level shifts.
- Bad, because requires `MacroIndicators` to carry a trailing series, which contradicts `pipeline_contracts.md §8a`.
- Bad, because `regime_lookback` as a config integer provides no guidance on how to retrieve or store the history.

### Percentile rank

- Good, because more robust than z-scores when the distribution is non-normal.
- Bad, because requires a reference dataset available at classification time.
- Bad, because same data-availability problem as z-scores in v0.

## More Information

When `MacroIndicators` is extended to carry a trailing series (or when A3 retrieves multiple historical data points), `classify_regime` should be updated to use the z-score approach from `design_decisions.md §1`, at which point `regime_lookback` becomes meaningful and the `RECOVERY` rule can be re-introduced. That change warrants a superseding ADR.

The `pmi` row above was revised by [ADR 0033](0033-pmi-regional-fed-composite.md): ISM removed its PMI from FRED, so `pmi` now sources a mean of regional Fed manufacturing diffusion indices (centered at 0) and its threshold is `0.0` rather than the ISM `50.0`.
