"""Fixtures used in integration tests."""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def integration_env() -> None:
    os.environ["MONEY_PIT__ALPACA_SERVICE"] = "alpaca-paper"
    os.environ["MONEY_PIT__ALPACA_USERNAME"] = "56kyle"
