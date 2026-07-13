"""Fixtures for the opt-in live paper-trade integration tier.

These fixtures resolve real credentials from the operator's config and keyring. They are only used by
@pytest.mark.live tests, which are deselected by default and additionally skipped unless MONEY_PIT_LIVE=1.
"""

from pathlib import Path

import pytest
from dotenv import load_dotenv

from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.config import load_config
from money_pit.config import resolve_alpaca_credentials


_PAPER_ENV_PATH: Path = Path(__file__).parents[2] / "config" / "paper.env"


@pytest.fixture
def live_config() -> Config:
    """Load the live-tier Config, pinning the Alpaca paper account when tests/config/paper.env is present.

    The override load makes paper.env authoritative over the operator's ambient MONEY_PIT__ALPACA_* and the
    integration_env placeholders, so the live tier always resolves the paper account even if the operator's
    user-level Alpaca config later changes.
    """
    if _PAPER_ENV_PATH.exists():
        _ = load_dotenv(_PAPER_ENV_PATH, override=True)
    return load_config()


@pytest.fixture
def live_credentials(live_config: Config) -> AlpacaCredentials:
    return resolve_alpaca_credentials(live_config)
