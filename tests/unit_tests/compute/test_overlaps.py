"""Tests for money_pit.compute.overlaps."""

import pytest

from money_pit.compute.overlaps import ETF_HOLDS_SINGLE_NAME_NOTE
from money_pit.compute.overlaps import detect_etf_overlaps
from money_pit.schemas.portfolio import CorrelatedOverlap
from money_pit.schemas.portfolio import Position


def _position(ticker: str) -> Position:
    return Position(
        ticker=ticker,
        quantity=1.0,
        cost_basis=100.0,
        current_value=100.0,
        unrealized_pl=0.0,
        sector="Tech",
        factor_tags=[],
    )


def test_detect_etf_overlaps_with_canonical_single_name() -> None:
    positions = [_position("SMH"), _position("NVDA")]
    etf_holdings = {"SMH": ["NVDA", "AVGO", "TSM"]}
    assert detect_etf_overlaps(positions, etf_holdings) == [
        CorrelatedOverlap(tickers=["SMH", "NVDA"], note=ETF_HOLDS_SINGLE_NAME_NOTE),
    ]


def test_detect_etf_overlaps_with_holding_not_held() -> None:
    positions = [_position("SMH")]
    etf_holdings = {"SMH": ["NVDA", "AVGO"]}
    assert detect_etf_overlaps(positions, etf_holdings) == []


def test_detect_etf_overlaps_with_empty_etf_holdings() -> None:
    positions = [_position("SMH"), _position("NVDA")]
    assert detect_etf_overlaps(positions, {}) == []


def test_detect_etf_overlaps_with_no_etf_positions() -> None:
    positions = [_position("NVDA"), _position("AAPL")]
    assert detect_etf_overlaps(positions, {}) == []


def test_detect_etf_overlaps_with_one_etf_holding_two_held_names_preserves_holdings_order() -> None:
    positions = [_position("SMH"), _position("NVDA"), _position("AVGO")]
    etf_holdings = {"SMH": ["AVGO", "TSM", "NVDA"]}
    assert detect_etf_overlaps(positions, etf_holdings) == [
        CorrelatedOverlap(tickers=["SMH", "AVGO"], note=ETF_HOLDS_SINGLE_NAME_NOTE),
        CorrelatedOverlap(tickers=["SMH", "NVDA"], note=ETF_HOLDS_SINGLE_NAME_NOTE),
    ]


def test_detect_etf_overlaps_with_two_etfs_preserves_insertion_order() -> None:
    positions = [_position("SMH"), _position("XLK"), _position("NVDA"), _position("AAPL")]
    etf_holdings = {"XLK": ["AAPL"], "SMH": ["NVDA"]}
    assert detect_etf_overlaps(positions, etf_holdings) == [
        CorrelatedOverlap(tickers=["XLK", "AAPL"], note=ETF_HOLDS_SINGLE_NAME_NOTE),
        CorrelatedOverlap(tickers=["SMH", "NVDA"], note=ETF_HOLDS_SINGLE_NAME_NOTE),
    ]


@pytest.mark.parametrize(
    ("held_ticker", "holding_symbol"),
    [
        ("NVDA", "nvda"),
        ("Nvda", "NVDA"),
        ("nvda", "NvDa"),
    ],
)
def test_detect_etf_overlaps_with_case_insensitive_match_uses_position_casing(
    held_ticker: str,
    holding_symbol: str,
) -> None:
    positions = [_position("SMH"), _position(held_ticker)]
    etf_holdings = {"SMH": [holding_symbol]}
    assert detect_etf_overlaps(positions, etf_holdings) == [
        CorrelatedOverlap(tickers=["SMH", held_ticker], note=ETF_HOLDS_SINGLE_NAME_NOTE),
    ]


def test_detect_etf_overlaps_with_self_holding_skipped() -> None:
    positions = [_position("SMH")]
    etf_holdings = {"SMH": ["SMH", "NVDA"]}
    assert detect_etf_overlaps(positions, etf_holdings) == []
