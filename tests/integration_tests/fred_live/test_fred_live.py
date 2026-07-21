"""Opt-in live smoke tests answering whether the operator's FRED API key is wired up correctly.

Every test here is marked `live` (deselected by default via addopts `-m 'not live'`) and additionally
skipped unless MONEY_PIT_LIVE=1, so a bare `-m live` run without an operator opt-in skips cleanly. The key
comes from `load_config()`, which reads MONEY_PIT__FRED_API_KEY from the environment and the user `.env`;
tests/config/paper.env is deliberately not loaded, since that file is the Alpaca paper account's and FRED
shares nothing with it. A missing key fails the tier loudly rather than skipping — a skip would answer "is
my key wired up" with a false green. Run explicitly with, e.g.,
`MONEY_PIT_LIVE=1 uv run pytest tests/integration_tests/fred_live -m live --no-cov -q`.

Both tests assert `FetchValue` specifically rather than merely "not FetchError". `fetch_fred_series` does
not call `raise_for_status()`, and FRED answers an invalid or unregistered key with HTTP 400 and a JSON
error body carrying no `observations`, which the parsing maps to `NoData` — so accepting `NoData` would
make this tier pass with a completely bogus key.
"""

import os

import pytest
from pydantic import SecretStr

from money_pit.config import Config
from money_pit.config import load_config
from money_pit.pipeline.orchestration import _DirectDeterministicTools
from money_pit.schemas.fetch_result import FetchResult
from money_pit.schemas.fetch_result import FetchValue


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("MONEY_PIT_LIVE") != "1",
        reason="live tier is opt-in; set MONEY_PIT_LIVE=1 (with network and a FRED key) to run",
    ),
]

_CORE_CPI_SERIES_ID: str = "CPILFESL"
_TREASURY_SPREAD_SERIES_ID: str = "T10Y2Y"


@pytest.fixture
def live_fred_api_key() -> SecretStr | None:
    config: Config = load_config()
    return config.fred_api_key


def test_fetch_fred_series_with_live_core_cpi(live_fred_api_key: SecretStr | None) -> None:
    """Fetch core CPI, a monthly series that is always populated, so a FetchValue here proves the key works."""
    assert live_fred_api_key is not None, (
        "refusing to run the FRED live tier without a key: set MONEY_PIT__FRED_API_KEY"
    )
    tools = _DirectDeterministicTools(fred_api_key=live_fred_api_key)

    result: FetchResult = tools.fetch_fred_series(_CORE_CPI_SERIES_ID)

    assert isinstance(result, FetchValue)
    assert isinstance(result.value, float)
    assert result.value > 0.0


def test_fetch_fred_series_with_live_treasury_spread(live_fred_api_key: SecretStr | None) -> None:
    """Fetch the 10y-2y spread, a daily series, with no sign assertion because the spread legitimately inverts.

    FRED publishes "." for a non-business day, which the parsing maps to NoData, so this can go red around
    market holidays. That stays a red rather than being loosened to accept NoData, which would make the test
    blind to a bad key.
    """
    assert live_fred_api_key is not None, (
        "refusing to run the FRED live tier without a key: set MONEY_PIT__FRED_API_KEY"
    )
    tools = _DirectDeterministicTools(fred_api_key=live_fred_api_key)

    result: FetchResult = tools.fetch_fred_series(_TREASURY_SPREAD_SERIES_ID)

    assert isinstance(result, FetchValue)
    assert isinstance(result.value, float)
