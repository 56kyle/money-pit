"""Module containing the deterministic factor-tag classifier and its FactorMetrics inputs for the money_pit package."""

from typing import Callable
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.config import Config
from money_pit.schemas.enums import FactorTag


class FactorMetrics(BaseModel):
    """Fundamental and price metrics for a single position, as sourced from yfinance."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    trailing_pe: float | None = None
    price_to_book: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    trailing_return: float | None = None
    return_on_equity: float | None = None
    profit_margin: float | None = None
    beta: float | None = None


def _at_most(value: float | None, threshold: float) -> bool:
    return value is not None and value <= threshold


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _is_value(metrics: FactorMetrics, config: Config) -> bool:
    return _at_most(metrics.trailing_pe, config.threshold_factor_value_pe) or _at_most(
        metrics.price_to_book, config.threshold_factor_value_pb
    )


def _is_growth(metrics: FactorMetrics, config: Config) -> bool:
    return _at_least(metrics.revenue_growth, config.threshold_factor_growth) or _at_least(
        metrics.earnings_growth, config.threshold_factor_growth
    )


def _is_momentum(metrics: FactorMetrics, config: Config) -> bool:
    return _at_least(metrics.trailing_return, config.threshold_factor_momentum)


def _is_quality(metrics: FactorMetrics, config: Config) -> bool:
    return _at_least(metrics.return_on_equity, config.threshold_factor_quality_roe) or _at_least(
        metrics.profit_margin, config.threshold_factor_quality_margin
    )


def _is_low_vol(metrics: FactorMetrics, config: Config) -> bool:
    return _at_most(metrics.beta, config.threshold_factor_low_vol_beta)


_FACTOR_RULES: dict[FactorTag, Callable[[FactorMetrics, Config], bool]] = {
    FactorTag.GROWTH: _is_growth,
    FactorTag.VALUE: _is_value,
    FactorTag.MOMENTUM: _is_momentum,
    FactorTag.QUALITY: _is_quality,
    FactorTag.LOW_VOL: _is_low_vol,
}


def classify_factors(metrics: FactorMetrics, config: Config) -> list[FactorTag]:
    """Map a position's metrics to its matched factor tags in FactorTag declaration order, failing soft on absent metrics."""
    return [tag for tag in FactorTag if _FACTOR_RULES[tag](metrics, config)]
