"""Fixtures used in integration tests."""

import os
from collections.abc import Iterator

import pytest

_INTEGRATION_ENV: dict[str, str] = {
    "MONEY_PIT__ALPACA_SERVICE": "alpaca-paper",
    "MONEY_PIT__ALPACA_USERNAME": "56kyle",
}


@pytest.fixture(scope="session", autouse=True)
def integration_env() -> Iterator[None]:
    snapshot: dict[str, str | None] = {name: os.environ.get(name) for name in _INTEGRATION_ENV}
    os.environ.update(_INTEGRATION_ENV)
    try:
        yield
    finally:
        for name, prior in snapshot.items():
            if prior is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prior
