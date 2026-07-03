"""Regime decision table: five indicators → RegimeTag (UNCERTAIN on missing/conflict)."""
from money_pit.config import Config
from money_pit.schemas.enums import RegimeTag
from money_pit.schemas.macro import MacroIndicators


_HIGHER_IS_BETTER: int = 1
_LOWER_IS_BETTER: int = -1


def discretize(value: float | None, threshold: float, orientation: int, band: float) -> int | None:
    """Map a scalar indicator reading to -1, 0, or +1 relative to its structural threshold."""
    if value is None:
        return None
    delta: float = (value - threshold) * orientation
    if delta > band:
        return 1
    if delta < -band:
        return -1
    return 0


def classify_regime(indicators: MacroIndicators, config: Config) -> RegimeTag:
    """Apply the v0 truth table to five discretized macro signals, returning a RegimeTag."""
    band: float = config.regime_band
    curve: int | None = discretize(
        indicators.yield_curve, config.threshold_yield_curve, _HIGHER_IS_BETTER, band
    )
    credit: int | None = discretize(
        indicators.credit_spreads, config.threshold_credit_spreads, _LOWER_IS_BETTER, band
    )
    pmi: int | None = discretize(
        indicators.pmi, config.threshold_pmi, _HIGHER_IS_BETTER, band
    )
    earnings: int | None = discretize(
        indicators.earnings_revisions, config.threshold_earnings_revisions, _HIGHER_IS_BETTER, band
    )
    inflation: int | None = discretize(
        indicators.inflation, config.threshold_inflation, _LOWER_IS_BETTER, band
    )

    if curve is None or credit is None or pmi is None or earnings is None or inflation is None:
        return RegimeTag.UNCERTAIN

    growth: int = pmi + earnings

    if curve == -1 and credit == -1 and pmi <= 0 and earnings <= 0:
        return RegimeTag.LATE_CYCLE_STRESS
    if credit <= 0 and pmi <= 0 and earnings <= 0 and inflation == -1:
        return RegimeTag.STAGFLATION
    if curve >= 0 and credit >= 0 and pmi == 1 and earnings == 1 and inflation >= 0:
        return RegimeTag.GROWTH_ACCELERATING
    if credit >= 0 and pmi <= 0 and earnings <= 0 and inflation >= 0:
        return RegimeTag.GROWTH_DECELERATING

    if (curve + credit) >= 1 and growth <= -1:
        return RegimeTag.UNCERTAIN
    if (curve + credit) <= -1 and growth >= 1:
        return RegimeTag.UNCERTAIN

    total: int = curve + credit + pmi + earnings
    if total > 1:
        return RegimeTag.GROWTH_ACCELERATING
    if total < -1:
        return RegimeTag.GROWTH_DECELERATING
    return RegimeTag.UNCERTAIN
