"""Confidence derivation from sources_used (primary/secondary/Brave rule)."""
from money_pit.schemas.enums import Confidence

_PRIMARY: frozenset[str] = frozenset({"fred_mcp", "edgartools_mcp", "alpaca_mcp"})
_SECONDARY: frozenset[str] = frozenset({"yfinance_mcp"})
_TERTIARY: frozenset[str] = frozenset({"brave_search_mcp"})


def derive_confidence(sources_used: list[str]) -> Confidence:
    if not sources_used:
        return Confidence.LOW
    used = set(sources_used)
    if used & _TERTIARY:
        return Confidence.LOW
    if used - _PRIMARY:
        return Confidence.MEDIUM
    return Confidence.HIGH
