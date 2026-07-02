"""Fixtures used in integration tests."""
import os

import pytest

from money_pit.config import load_config


@pytest.fixture(scope="session", autouse=True)
def integration_env() -> None:
    os.environ["MONEY_PIT__ALPACA_SERVICE"] = "alpaca_paper"
    os.environ["MONEY_PIT__ALPACA_USERNAME"] = "56kyle"
    load_config.cache_clear()
