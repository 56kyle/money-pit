"""Fixtures used in all tests.

Session-scoped fixtures describing money-pit's repository layout. Each fixture
falls back to a conventional default path but can be overridden per-test via
indirect parametrization (`request.param`), matching the `__`-suffixed
param-default idiom used elsewhere in this test suite.
"""
from pathlib import Path

import pytest
from pytest import FixtureRequest


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
