"""Fixtures for the opt-in live paper-trade acceptance tier.

These fixtures resolve real credentials from the operator's config and keyring. They are only used by
@pytest.mark.live tests, which are deselected by default and additionally skipped unless MONEY_PIT_LIVE=1.
"""

import pytest

from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.config import load_config
from money_pit.config import resolve_alpaca_credentials


@pytest.fixture
def live_config() -> Config:
    load_config.cache_clear()
    return load_config()


@pytest.fixture
def live_credentials(live_config: Config) -> AlpacaCredentials:
    return resolve_alpaca_credentials(live_config)
