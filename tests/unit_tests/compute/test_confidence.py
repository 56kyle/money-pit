"""Tests for money_pit.compute.confidence."""
import pytest

from money_pit.compute.confidence import derive_confidence
from money_pit.schemas.enums import Confidence


@pytest.mark.parametrize(
    "sources_used,expected",
    [
        ([], Confidence.LOW),
        (["fred_mcp"], Confidence.HIGH),
        (["edgartools_mcp", "alpaca_mcp"], Confidence.HIGH),
        (["fred_mcp", "yfinance_mcp"], Confidence.MEDIUM),
        (["yfinance_mcp"], Confidence.MEDIUM),
        (["brave_search_mcp"], Confidence.LOW),
        (["fred_mcp", "brave_search_mcp"], Confidence.LOW),
    ],
)
def test_derive_confidence_with_various_sources(sources_used: list[str], expected: Confidence) -> None:
    assert derive_confidence(sources_used) == expected
