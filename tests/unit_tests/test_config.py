"""Tests for money_pit.config's credential resolution — the fail-closed keyring + paper/live routing gates.

Pins _paper_from_service's suffix routing, resolve_alpaca_credentials' keyring-backed resolution, and
resolve_gmail_app_password's fail-closed behavior. A real in-memory keyring backend stands in for the OS
keyring (no mocks). Assertions target the CredentialResolutionError TYPE and enum/field values, never text.
"""

import pytest
from pytest import FixtureRequest

from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.config import _paper_from_service
from money_pit.config import resolve_alpaca_credentials
from money_pit.config import resolve_gmail_app_password
from tests.unit_tests.conftest import InMemoryKeyring


@pytest.fixture
def config__alpaca_service(request: FixtureRequest) -> str:
    return getattr(request, "param", "alpaca-paper")


@pytest.fixture
def config__alpaca_username(request: FixtureRequest) -> str:
    return getattr(request, "param", "alpaca-api-key")


@pytest.fixture
def config__gmail_address(request: FixtureRequest) -> str | None:
    return getattr(request, "param", "sender@gmail.com")


@pytest.fixture
def config__secret_key(request: FixtureRequest) -> str:
    return getattr(request, "param", "alpaca-secret-key")


@pytest.fixture
def config__gmail_app_password(request: FixtureRequest) -> str:
    return getattr(request, "param", "gmail-app-password")


@pytest.fixture
def config(
    request: FixtureRequest,
    config__alpaca_service: str,
    config__alpaca_username: str,
    config__gmail_address: str | None,
) -> Config:
    return getattr(
        request,
        "param",
        Config(
            alpaca_service=config__alpaca_service,
            alpaca_username=config__alpaca_username,
            gmail_address=config__gmail_address,
        ),
    )


@pytest.mark.parametrize(("service", "expected"), [("alpaca-paper", True), ("alpaca-live", False)])
def test__paper_from_service_with_valid(service: str, expected: bool) -> None:
    assert _paper_from_service(service) is expected


def test__paper_from_service_with_unrecognized_suffix() -> None:
    with pytest.raises(CredentialResolutionError):
        _ = _paper_from_service("alpaca-sandbox")


def test_resolve_alpaca_credentials_with_valid(
    config: Config, config__secret_key: str, in_memory_keyring: InMemoryKeyring
) -> None:
    in_memory_keyring.set_password(config.alpaca_service, config.alpaca_username, config__secret_key)

    result: AlpacaCredentials = resolve_alpaca_credentials(config)

    assert result == AlpacaCredentials(
        api_key=config.alpaca_username, secret_key=config__secret_key, paper=True
    )


def test_resolve_alpaca_credentials_with_missing_secret(config: Config, in_memory_keyring: InMemoryKeyring) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = resolve_alpaca_credentials(config)


@pytest.mark.parametrize("config__alpaca_service", ["alpaca-sandbox"], indirect=True)
def test_resolve_alpaca_credentials_with_ambiguous_service(
    config: Config, config__secret_key: str, in_memory_keyring: InMemoryKeyring
) -> None:
    in_memory_keyring.set_password(config.alpaca_service, config.alpaca_username, config__secret_key)

    with pytest.raises(CredentialResolutionError):
        _ = resolve_alpaca_credentials(config)


def test_resolve_gmail_app_password_with_valid(
    config: Config, config__gmail_app_password: str, in_memory_keyring: InMemoryKeyring
) -> None:
    assert config.gmail_address is not None
    in_memory_keyring.set_password(config.gmail_service, config.gmail_address, config__gmail_app_password)

    assert resolve_gmail_app_password(config) == config__gmail_app_password


@pytest.mark.parametrize("config__gmail_address", [None], indirect=True)
def test_resolve_gmail_app_password_with_no_address(config: Config, in_memory_keyring: InMemoryKeyring) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = resolve_gmail_app_password(config)


def test_resolve_gmail_app_password_with_missing_secret(config: Config, in_memory_keyring: InMemoryKeyring) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = resolve_gmail_app_password(config)
