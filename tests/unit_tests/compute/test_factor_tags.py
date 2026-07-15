"""Tests for money_pit.compute.factor_tags."""

import pytest

from money_pit.compute.factor_tags import FactorMetrics
from money_pit.compute.factor_tags import classify_factors
from money_pit.config import Config
from money_pit.schemas.enums import FactorTag


@pytest.fixture
def config() -> Config:
    return Config(alpaca_service="stub", alpaca_username="stub", alpaca_paper=True)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("trailing_pe", 20.0, [FactorTag.VALUE]),
        ("trailing_pe", 20.01, []),
        ("price_to_book", 2.0, [FactorTag.VALUE]),
        ("price_to_book", 2.01, []),
    ],
)
def test_classify_factors_with_value(field: str, value: float, expected: list[FactorTag], config: Config) -> None:
    assert classify_factors(FactorMetrics(**{field: value}), config) == expected


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("revenue_growth", 0.15, [FactorTag.GROWTH]),
        ("revenue_growth", 0.14, []),
        ("earnings_growth", 0.15, [FactorTag.GROWTH]),
        ("earnings_growth", 0.14, []),
    ],
)
def test_classify_factors_with_growth(field: str, value: float, expected: list[FactorTag], config: Config) -> None:
    assert classify_factors(FactorMetrics(**{field: value}), config) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.10, [FactorTag.MOMENTUM]),
        (0.09, []),
    ],
)
def test_classify_factors_with_momentum(value: float, expected: list[FactorTag], config: Config) -> None:
    assert classify_factors(FactorMetrics(trailing_return=value), config) == expected


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("return_on_equity", 0.15, [FactorTag.QUALITY]),
        ("return_on_equity", 0.14, []),
        ("profit_margin", 0.15, [FactorTag.QUALITY]),
        ("profit_margin", 0.14, []),
    ],
)
def test_classify_factors_with_quality(field: str, value: float, expected: list[FactorTag], config: Config) -> None:
    assert classify_factors(FactorMetrics(**{field: value}), config) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.90, [FactorTag.LOW_VOL]),
        (0.91, []),
    ],
)
def test_classify_factors_with_low_vol(value: float, expected: list[FactorTag], config: Config) -> None:
    assert classify_factors(FactorMetrics(beta=value), config) == expected


def test_classify_factors_with_all_factors_satisfied_returns_enum_order(config: Config) -> None:
    metrics = FactorMetrics(
        revenue_growth=0.20,
        trailing_pe=15.0,
        trailing_return=0.20,
        return_on_equity=0.20,
        beta=0.50,
    )
    assert classify_factors(metrics, config) == [
        FactorTag.GROWTH,
        FactorTag.VALUE,
        FactorTag.MOMENTUM,
        FactorTag.QUALITY,
        FactorTag.LOW_VOL,
    ]


def test_classify_factors_with_subset_preserves_enum_order(config: Config) -> None:
    metrics = FactorMetrics(return_on_equity=0.20, trailing_pe=15.0)
    assert classify_factors(metrics, config) == [FactorTag.VALUE, FactorTag.QUALITY]


def test_classify_factors_with_all_none_returns_empty(config: Config) -> None:
    assert classify_factors(FactorMetrics(), config) == []


def test_classify_factors_with_partial_none_operand_still_fires(config: Config) -> None:
    metrics = FactorMetrics(trailing_pe=None, price_to_book=1.0)
    assert classify_factors(metrics, config) == [FactorTag.VALUE]
