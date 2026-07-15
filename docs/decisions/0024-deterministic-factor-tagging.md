# Deterministic per-position factor tagging from yfinance metrics

- Status: accepted
- Date: 2026-07-15
- Deciders: owner, python-dev

## Context and Problem Statement

The portfolio snapshot ships per-position `factor_tags` (`growth | value | momentum | quality | low_vol`,
`pipeline_contracts.md` §0/§8). Agent 4's Step 3.2 "derives the portfolio factor exposure profile _from the
tags on each position_" (`agent_4.md:82`, `pipeline_contracts.md` §8 reconciliation #2), and
`compute/factor_profile.py` aggregates those tags into the five-factor profile. But `alpaca_portfolio.py`
emitted every position's `factor_tags=[]` (v0-deferred), so A4's factor reasoning ran on empty input.

The spec names the five factors and pins the aggregation as **code**, but **never defines the classification
rule** — the mapping from a holding's metrics to which tags it earns. This is the gap that was blocking real
factor data. (Contrast the macro regime, which has a full z-score decision table in `design_decisions.md`
§1; the per-position factor mapping had no equivalent.)

Two things had to be decided: **where the classification runs** (deterministic code vs. an LLM/VLM), and
**what the rule is**.

## Decision Drivers

- The architecture's deterministic-split ethos: "Every deterministic computation is a function or a tool,
  never model output" (`architecture.md` §9/§35); factor *aggregation* is already assigned to code
  (`pipeline_contracts.md` §9). A classification that reads numeric fundamentals is a deterministic transform,
  not open-ended language — the same argument that keeps regime tagging and sizing out of the model.
- yfinance is already a dependency and already fetched per position (`alpaca_portfolio._resolve_sector`),
  exposing the needed fundamentals — no new library or data source.
- Threshold-driven classification must be tunable without code edits, matching the existing `threshold_*`
  regime knobs on `Config`.
- Fundamentals are frequently missing per ticker; the classifier must degrade, never raise.

## Decision Outcome

1. **Deterministic, code-based classification** — `compute/factor_tags.py::classify_factors`, a pure
   function over a `FactorMetrics` value object. No LLM/VLM in the path. (`implementation_plan.md` had
   floated "VLM/reference-data sourcing"; rejected — nothing in the deterministic-split tables routes factor
   tagging to a model, and the inputs are plain numbers.)
2. **A decision table over yfinance `.info` metrics**, one predicate per factor, thresholds as `Config`
   knobs (v0-calibration values, tunable). Inclusive comparisons; a position may earn multiple tags:
   - **value**: `trailing_pe ≤ threshold_factor_value_pe (20.0)` or `price_to_book ≤ threshold_factor_value_pb (2.0)`
   - **growth**: `revenue_growth ≥ threshold_factor_growth (0.15)` or `earnings_growth ≥ threshold_factor_growth`
   - **momentum**: `trailing_return ≥ threshold_factor_momentum (0.10)` (from `.info`'s `52WeekChange`)
   - **quality**: `return_on_equity ≥ threshold_factor_quality_roe (0.15)` or `profit_margin ≥ threshold_factor_quality_margin (0.15)`
   - **low_vol**: `beta ≤ threshold_factor_low_vol_beta (0.90)`
3. **Fail-soft on absent metrics.** A `None` operand never satisfies a comparison; a factor whose operands
   are all `None` is simply absent. `classify_factors` never raises on missing data — the same degradation
   contract as `_resolve_sector`'s `"unknown"` fallback.
4. **Absolute per-metric thresholds, not cross-sectional ranks.** At the current single-account scale there
   is no universe to rank against, so each holding is classified on its own absolute metrics.

Tags are returned in `FactorTag` declaration order so the output is canonical regardless of rule-table order.

### Consequences

Good: A4's Step-3 factor reasoning now runs on real per-position tags; `aggregate_factor_profile` produces a
real profile instead of all-zeros. The rule is transparent, deterministic, unit-tested at every threshold
boundary, tunable via `MONEY_PIT__THRESHOLD_FACTOR_*`, and adds no dependency. yfinance gaps degrade a
position to fewer (or no) tags rather than failing the snapshot.

Bad / to watch: **absolute thresholds are a v0 calibration** — 20.0 P/E as "value" etc. are defensible but
not tuned against any backtest, and a name near a boundary can flip. They are cross-sectionally naive (a
low-P/E sector looks uniformly "value"). Because the tags feed A4's *judgment* (not a hard clamp), a
mis-tag biases the model's narrative rather than directly mis-sizing capital — an acceptable blast radius
for a v0 rule. Revisit with a factor library (`quantstats`/`riskfolio-lib`, documented optional in
`architecture.md` §8) or cross-sectional z-scores if the calibration proves too coarse.

## Considered Options (key rejections)

- **LLM/VLM classification.** Rejected: non-deterministic, one paid call per holding, and contradicts the
  deterministic-split the spec applies to every other post-processor computation. The inputs are numeric
  fundamentals — no language judgment is involved.
- **A factor library (riskfolio-lib / quantstats / empyrical).** Rejected for now: documented as *optional*
  (`architecture.md` §8) and not installed; unnecessary for a threshold classifier and would add a
  dependency for no capability the `.info` fields don't already provide.
- **Static ticker→factors reference file.** Rejected: manual, goes stale, and can't classify an arbitrary
  holding the account happens to acquire.
- **Cross-sectional ranking.** Deferred: more principled, but there is no universe to rank against at
  single-account scale.

## More Information

Resolves the unspecified per-position factor-classification rule referenced by `pipeline_contracts.md`
§8/§9 and `agent_4.md` Step 3.2. Implements `src/money_pit/compute/factor_tags.py`
(`FactorMetrics`, `classify_factors`) and the `threshold_factor_*` knobs on `src/money_pit/config.py`;
consumed by `src/money_pit/alpaca_portfolio.py` (the snapshot builder) and aggregated by
`src/money_pit/compute/factor_profile.py`. The companion `correlated_overlaps` sourcing and its
sizing clamp are a separate decision (ADR 0025). The flat sector-headroom gap discovered during this work
(`analysis.py` sector clamp ignores per-sector exposure) is tracked as a follow-up, not resolved here.
