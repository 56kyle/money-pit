"""Tests for money_pit.market_data's pure yfinance-info mappers.

as_float, resolve_sector, and is_etf coerce a ticker's pre-fetched yfinance `.info` mapping into
finite floats, a sector string, and an ETF flag. They are pure and fail-soft, so no monkeypatching is
needed; the network fetchers (fetch_ticker_info/fetch_etf_holdings) are live-only and unpinned here.
Assertions target concrete values and the module's UNKNOWN_SECTOR fallback constant.
"""

import pytest

from money_pit.market_data import UNKNOWN_SECTOR
from money_pit.market_data import as_float
from money_pit.market_data import is_etf
from money_pit.market_data import resolve_sector


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5.0),
        (0, 0.0),
        (2.5, 2.5),
        (-3.5, -3.5),
    ],
)
def test_as_float_with_valid(value: object, expected: float) -> None:
    assert as_float(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "abc",
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_as_float_with_invalid(value: object) -> None:
    assert as_float(value) is None


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"sector": "Technology"}, "Technology"),
        ({}, UNKNOWN_SECTOR),
        ({"sector": ""}, UNKNOWN_SECTOR),
        ({"sector": 5}, UNKNOWN_SECTOR),
    ],
)
def test_resolve_sector(info: dict[str, object], expected: str) -> None:
    assert resolve_sector(info) == expected


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"quoteType": "ETF"}, True),
        ({"quoteType": "etf"}, True),
        ({}, False),
        ({"quoteType": "EQUITY"}, False),
        ({"quoteType": 5}, False),
    ],
)
def test_is_etf(info: dict[str, object], expected: bool) -> None:
    assert is_etf(info) is expected
