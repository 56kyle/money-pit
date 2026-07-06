"""Tests for money_pit.compute.confidence."""

import pytest

from money_pit.compute.confidence import derive_confidence
from money_pit.schemas.enums import Confidence, DataSourceToken


@pytest.mark.parametrize(
    ("sources_used", "expected"),
    [
        ([DataSourceToken.FRED_MCP, DataSourceToken.EDGARTOOLS_MCP], Confidence.HIGH),
        ([DataSourceToken.ALPACA_MCP], Confidence.HIGH),
        ([DataSourceToken.YFINANCE_MCP], Confidence.MEDIUM),
        ([DataSourceToken.FRED_MCP, DataSourceToken.YFINANCE_MCP], Confidence.MEDIUM),
        ([DataSourceToken.BRAVE_SEARCH_MCP], Confidence.LOW),
        ([DataSourceToken.FRED_MCP, DataSourceToken.BRAVE_SEARCH_MCP], Confidence.LOW),
        ([], Confidence.LOW),
    ],
)
def test_derive_confidence(sources_used: list[DataSourceToken], expected: Confidence) -> None:
    assert derive_confidence(sources_used) == expected
