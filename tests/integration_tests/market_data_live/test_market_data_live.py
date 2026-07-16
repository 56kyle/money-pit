"""Opt-in live smoke tests for the real yfinance-backed instrument resolver.

Every test here is marked `live` (deselected by default via addopts `-m 'not live'`) and additionally
skipped unless MONEY_PIT_LIVE=1, so a bare `-m live` run without an operator opt-in skips cleanly. These
tests need only network — no credentials and no capital — and confirm the InstrumentFacts shape holds
against the real yfinance API. Run explicitly with, e.g., `nox -s tests-python -- -m live` and MONEY_PIT_LIVE=1 set.
"""

import os

import pytest

from money_pit.contracts import ResolveInstrumentFacts
from money_pit.market_data import make_yfinance_instrument_resolver
from money_pit.schemas.instrument import InstrumentFacts


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("MONEY_PIT_LIVE") != "1",
        reason="live tier is opt-in; set MONEY_PIT_LIVE=1 (with network) to run",
    ),
]


def test_live_resolve_etf_shape() -> None:
    resolve: ResolveInstrumentFacts = make_yfinance_instrument_resolver()

    facts: InstrumentFacts = resolve("SMH")

    assert facts.is_etf is True
    assert isinstance(facts.holdings, list)
    assert facts.holdings
    assert all(isinstance(symbol, str) for symbol in facts.holdings)
    assert isinstance(facts.sector, str)
    assert facts.sector


def test_live_resolve_single_name_shape() -> None:
    resolve: ResolveInstrumentFacts = make_yfinance_instrument_resolver()

    facts: InstrumentFacts = resolve("AAPL")

    assert facts.is_etf is False
    assert facts.holdings == []
    assert isinstance(facts.sector, str)
    assert facts.sector
