"""Fixtures used in all tests.

Session-scoped fixtures describing money-pit's repository layout. Each fixture
falls back to a conventional default path but can be overridden per-test via
indirect parametrization (`request.param`), matching the `__`-suffixed
param-default idiom used elsewhere in this test suite.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest import FixtureRequest

from money_pit.config import ENV_PREFIX


@pytest.fixture(autouse=True)
def _restore_money_pit_env() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # Pytest discovers this autouse fixture dynamically.
    """Bracket every test so MONEY_PIT__* env mutations cannot cross a test boundary, across all tiers.

    load_config calls load_dotenv, which writes a .env's keys into os.environ persistently and untracked by
    monkeypatch. Snapshotting the ENV_PREFIX keys before the test and restoring them exactly afterwards keeps
    that pollution (e.g. a mistyped MONEY_PIT__SMTP_PORT from a unit test) from leaking into a later tier's
    Config() construction. As a root-level autouse fixture it wraps lower conftests' env fixtures, so the unit
    _clear_money_pit_env still clears for hermetic defaults inside this snapshot's bracket, and integration_env's
    session vars sit inside the snapshot and are preserved.
    """
    snapshot: dict[str, str] = {name: value for name, value in os.environ.items() if name.startswith(ENV_PREFIX)}
    try:
        yield
    finally:
        for name in [name for name in os.environ if name.startswith(ENV_PREFIX)]:
            if name not in snapshot:
                del os.environ[name]
        for name, value in snapshot.items():
            os.environ[name] = value


UNCONFIGURED_EMAIL_REASON: str = "no gmail_address is set"
"""Reason handed to make_unconfigured_email_sender wherever a test drives the never-configured mailbox path."""


_TESTS_FOLDER_NAME: str = "tests"
_UNIT_TESTS_FOLDER_NAME: str = "unit_tests"
_INTEGRATION_TESTS_FOLDER_NAME: str = "integration_tests"
_ACCEPTANCE_TESTS_FOLDER_NAME: str = "acceptance_tests"
_DATA_FOLDER_NAME: str = "data"


@pytest.fixture(scope="session")
def repository_root(request: FixtureRequest) -> Path:
    """Path to the repository's root folder."""
    return getattr(request, "param", request.config.rootpath)


@pytest.fixture(scope="session")
def tests_folder(request: FixtureRequest, repository_root: Path) -> Path:
    """Path to the top-level tests folder."""
    return getattr(request, "param", repository_root / _TESTS_FOLDER_NAME)


@pytest.fixture(scope="session")
def unit_tests_folder(request: FixtureRequest, tests_folder: Path) -> Path:
    """Path to the folder containing unit tests."""
    return getattr(request, "param", tests_folder / _UNIT_TESTS_FOLDER_NAME)


@pytest.fixture(scope="session")
def integration_tests_folder(request: FixtureRequest, tests_folder: Path) -> Path:
    """Path to the folder containing integration tests."""
    return getattr(request, "param", tests_folder / _INTEGRATION_TESTS_FOLDER_NAME)


@pytest.fixture(scope="session")
def acceptance_tests_folder(request: FixtureRequest, tests_folder: Path) -> Path:
    """Path to the folder containing acceptance tests."""
    return getattr(request, "param", tests_folder / _ACCEPTANCE_TESTS_FOLDER_NAME)


@pytest.fixture(scope="session")
def data_folder(request: FixtureRequest, tests_folder: Path) -> Path:
    """Path to the folder containing shared test data fixtures."""
    return getattr(request, "param", tests_folder / _DATA_FOLDER_NAME)
