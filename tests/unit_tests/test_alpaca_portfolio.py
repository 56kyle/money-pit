"""Tests for money_pit.alpaca_portfolio's pure mappers — _to_position and _sector_weights.

_to_position maps one alpaca-py raw position onto our frozen Position and fails closed on non-equity assets;
sector resolution is monkeypatched to a deterministic value so no yfinance network call happens. The
alpaca-py TradingClient calls inside make_alpaca_portfolio_fetcher are live-only and pinned by the
acceptance tier. Assertions target the NonEquityPositionError TYPE and concrete field values.
"""

from dataclasses import dataclass

import pytest
from pytest import MonkeyPatch

from money_pit import alpaca_portfolio
from money_pit.alpaca_portfolio import NonEquityPositionError
from money_pit.alpaca_portfolio import _sector_weights
from money_pit.alpaca_portfolio import _to_position
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
def deterministic_sector(monkeypatch: MonkeyPatch) -> str:
    sector = "Technology"
    monkeypatch.setattr(alpaca_portfolio, "_resolve_sector", lambda _ticker: sector)
    return sector


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


def test__to_position_with_equity(raw_position: _RawPosition, deterministic_sector: str) -> None:
    position = _to_position(raw_position)

    assert position == Position(
        ticker="NVDA",
        quantity=10.0,
        cost_basis=100.0,
        current_value=1500.0,
        unrealized_pl=500.0,
        sector=deterministic_sector,
        factor_tags=[],
    )


def test__to_position_with_non_equity(raw_position: _RawPosition, deterministic_sector: str) -> None:
    raw_position.asset_class = "crypto"

    with pytest.raises(NonEquityPositionError):
        _ = _to_position(raw_position)


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
