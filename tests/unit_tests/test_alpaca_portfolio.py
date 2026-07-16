"""Tests for money_pit.alpaca_portfolio's pure mappers.

The mappers under test (_factor_metrics_from_info, _to_position, _sector_weights) are pure and take
plain dicts / a pre-fetched `.info` mapping / alpaca-py raw positions, so no monkeypatching is needed.
_to_position maps one alpaca-py raw position onto our frozen Position and fails closed on non-equity
assets. The alpaca-py TradingClient calls inside make_alpaca_portfolio_fetcher are live-only and
pinned by the live tier. Assertions target the NonEquityPositionError TYPE and concrete field/model
values.
"""

from dataclasses import dataclass

import pytest

from money_pit.alpaca_portfolio import NonEquityPositionError
from money_pit.alpaca_portfolio import _factor_metrics_from_info
from money_pit.alpaca_portfolio import _sector_weights
from money_pit.alpaca_portfolio import _to_position
from money_pit.compute.factor_tags import FactorMetrics
from money_pit.config import Config
from money_pit.schemas.enums import FactorTag
from money_pit.schemas.portfolio import Position


@dataclass
class _RawPosition:
    """A hand-written stand-in for an alpaca-py Position, carrying only the attributes _to_position reads."""

    symbol: str
    qty: str
    avg_entry_price: str
    market_value: str
    unrealized_pl: str
    asset_class: str


@pytest.fixture
def config() -> Config:
    return Config(alpaca_service="stub", alpaca_username="stub", alpaca_paper=True)


@pytest.fixture
def raw_position() -> _RawPosition:
    return _RawPosition(
        symbol="NVDA",
        qty="10",
        avg_entry_price="100.0",
        market_value="1500.0",
        unrealized_pl="500.0",
        asset_class="us_equity",
    )


@pytest.fixture
def equity_info() -> dict[str, object]:
    return {
        "sector": "Technology",
        "trailingPE": 15.0,
        "priceToBook": 3.0,
        "revenueGrowth": 0.05,
        "earningsGrowth": 0.02,
        "52WeekChange": 0.03,
        "returnOnEquity": 0.10,
        "profitMargins": 0.08,
        "beta": 1.20,
    }


def _position(*, sector: str, current_value: float) -> Position:
    return Position(
        ticker="X",
        quantity=1.0,
        cost_basis=1.0,
        current_value=current_value,
        unrealized_pl=0.0,
        sector=sector,
        factor_tags=[],
    )


def test__factor_metrics_from_info_with_full_info(equity_info: dict[str, object]) -> None:
    assert _factor_metrics_from_info(equity_info) == FactorMetrics(
        trailing_pe=15.0,
        price_to_book=3.0,
        revenue_growth=0.05,
        earnings_growth=0.02,
        trailing_return=0.03,
        return_on_equity=0.10,
        profit_margin=0.08,
        beta=1.20,
    )


def test__factor_metrics_from_info_with_empty_info() -> None:
    assert _factor_metrics_from_info({}) == FactorMetrics()


def test__factor_metrics_from_info_with_non_finite_value() -> None:
    metrics = _factor_metrics_from_info({"52WeekChange": float("nan")})

    assert metrics.trailing_return is None


def test__to_position_with_equity(
    raw_position: _RawPosition, equity_info: dict[str, object], config: Config
) -> None:
    position = _to_position(raw_position, equity_info, config)

    assert position == Position(
        ticker="NVDA",
        quantity=10.0,
        cost_basis=100.0,
        current_value=1500.0,
        unrealized_pl=500.0,
        sector="Technology",
        factor_tags=[FactorTag.VALUE],
    )


def test__to_position_with_non_equity(
    raw_position: _RawPosition, equity_info: dict[str, object], config: Config
) -> None:
    raw_position.asset_class = "crypto"

    with pytest.raises(NonEquityPositionError):
        _ = _to_position(raw_position, equity_info, config)


def test__sector_weights_with_positive_total() -> None:
    positions = [
        _position(sector="Technology", current_value=6000.0),
        _position(sector="Technology", current_value=2000.0),
        _position(sector="Health Care", current_value=4000.0),
    ]

    weights = _sector_weights(positions, total_account_value=40_000.0)

    assert weights == pytest.approx({"Technology": 0.2, "Health Care": 0.1})


def test__sector_weights_with_non_positive_total() -> None:
    positions = [_position(sector="Technology", current_value=6000.0)]

    assert _sector_weights(positions, total_account_value=0.0) == {}
