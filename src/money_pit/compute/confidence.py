"""Confidence derivation from sources_used (primary/secondary/Brave rule)."""

from money_pit.schemas.enums import Confidence
from money_pit.schemas.enums import DataSourceToken


_PRIMARY: frozenset[DataSourceToken] = frozenset(
    {
        DataSourceToken.FRED_MCP,
        DataSourceToken.EDGARTOOLS_MCP,
        DataSourceToken.ALPACA_MCP,
    }
)
_TERTIARY: frozenset[DataSourceToken] = frozenset({DataSourceToken.BRAVE_SEARCH_MCP})


def derive_confidence(sources_used: list[DataSourceToken]) -> Confidence:
    if not sources_used:
        return Confidence.LOW
    used: set[DataSourceToken] = set(sources_used)
    if used & _TERTIARY:
        return Confidence.LOW
    if used - _PRIMARY:
        return Confidence.MEDIUM
    return Confidence.HIGH
