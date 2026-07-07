"""Tests for money_pit.config's credential resolution — the fail-closed keyring + paper/live routing gates.

Pins _paper_from_service's suffix routing, resolve_alpaca_credentials' keyring-backed resolution, and
resolve_gmail_app_password's fail-closed behavior. A real in-memory keyring backend stands in for the OS
keyring (no mocks). Assertions target the CredentialResolutionError TYPE and enum/field values, never text.
"""

from pathlib import Path

import pytest
from pydantic import SecretStr
from pydantic import ValidationError
from pytest import FixtureRequest
from pytest import MonkeyPatch

from money_pit.config import DEFAULT_OWNER_RECIPIENT
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.config import _missing_required_env_vars
from money_pit.config import _paper_from_service
from money_pit.config import load_config
from money_pit.config import resolve_alpaca_credentials
from money_pit.config import resolve_gmail_app_password
from money_pit.constants import DEFAULT_SMTP_HOST
from money_pit.constants import DEFAULT_SMTP_PORT
from money_pit.constants import GMAIL_KEYRING_SERVICE
from tests.unit_tests.conftest import InMemoryKeyring


_REQUIRED_ENV: str = "MONEY_PIT__ALPACA_SERVICE=alpaca-paper\nMONEY_PIT__ALPACA_USERNAME=alpaca-api-key\n"


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
def secret_key(request: FixtureRequest) -> str:
    return getattr(request, "param", "alpaca-secret-key")


@pytest.fixture
def gmail_app_password(request: FixtureRequest) -> str:
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
    config: Config, secret_key: str, in_memory_keyring: InMemoryKeyring
) -> None:
    in_memory_keyring.set_password(config.alpaca_service, config.alpaca_username, secret_key)

    result: AlpacaCredentials = resolve_alpaca_credentials(config)

    assert result == AlpacaCredentials(
        api_key=config.alpaca_username, secret_key=secret_key, paper=True
    )


def test_resolve_alpaca_credentials_with_missing_secret(config: Config, in_memory_keyring: InMemoryKeyring) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = resolve_alpaca_credentials(config)


@pytest.mark.parametrize("config__alpaca_service", ["alpaca-sandbox"], indirect=True)
def test_resolve_alpaca_credentials_with_ambiguous_service(
    config: Config, secret_key: str, in_memory_keyring: InMemoryKeyring
) -> None:
    in_memory_keyring.set_password(config.alpaca_service, config.alpaca_username, secret_key)

    with pytest.raises(CredentialResolutionError):
        _ = resolve_alpaca_credentials(config)


def test_resolve_gmail_app_password_with_valid(
    config: Config, gmail_app_password: str, in_memory_keyring: InMemoryKeyring
) -> None:
    assert config.gmail_address is not None
    in_memory_keyring.set_password(config.gmail_service, config.gmail_address, gmail_app_password)

    assert resolve_gmail_app_password(config) == gmail_app_password


@pytest.mark.parametrize("config__gmail_address", [None], indirect=True)
def test_resolve_gmail_app_password_with_no_address(config: Config, in_memory_keyring: InMemoryKeyring) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = resolve_gmail_app_password(config)


def test_resolve_gmail_app_password_with_missing_secret(config: Config, in_memory_keyring: InMemoryKeyring) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = resolve_gmail_app_password(config)


@pytest.fixture
def env_file__content(request: FixtureRequest) -> str:
    return getattr(request, "param", _REQUIRED_ENV)


@pytest.fixture
def env_file(tmp_path: Path, env_file__content: str) -> Path:
    path: Path = tmp_path / ".env"
    _ = path.write_text(env_file__content, encoding="utf-8")
    return path


def test_load_config_with_valid(env_file: Path) -> None:
    config: Config = load_config(env_file)

    assert config.alpaca_service == "alpaca-paper"
    assert config.alpaca_username == "alpaca-api-key"


def test__missing_required_env_vars() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _ = Config()

    missing: list[str] = _missing_required_env_vars(exc_info.value)

    assert missing == ["MONEY_PIT__ALPACA_SERVICE", "MONEY_PIT__ALPACA_USERNAME"]


@pytest.mark.parametrize("env_file__content", ["MONEY_PIT__ALPACA_SERVICE=alpaca-paper\n"], indirect=True)
def test_load_config_with_missing_required_var(env_file: Path) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = load_config(env_file)


@pytest.mark.parametrize(
    "env_file__content",
    [
        "MONEY_PIT__ALPACA_SERVICE=alpaca-paper\n"
        "MONEY_PIT__ALPACA_USERNAME=alpaca-api-key\n"
        "MONEY_PIT__SMTP_PORT=not-a-port\n"
    ],
    indirect=True,
)
def test_load_config_with_mistyped_var(env_file: Path) -> None:
    with pytest.raises(ValidationError):
        _ = load_config(env_file)


def test_load_config_with_env_override(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MONEY_PIT__ALPACA_SERVICE", "alpaca-paper")
    monkeypatch.setenv("MONEY_PIT__ALPACA_USERNAME", "alpaca-api-key")
    monkeypatch.setenv("MONEY_PIT__LLM_MODEL", "override-model")

    config: Config = load_config(tmp_path / "absent.env")

    assert config.llm_model == "override-model"


def test_Config_with_defaults() -> None:
    config: Config = Config(alpaca_service="alpaca-paper", alpaca_username="alpaca-api-key")

    assert config.llm_model == "claude-sonnet-5"
    assert config.gmail_address is None
    assert config.gmail_service == GMAIL_KEYRING_SERVICE
    assert config.owner_recipient == DEFAULT_OWNER_RECIPIENT
    assert config.smtp_host == DEFAULT_SMTP_HOST
    assert config.smtp_port == DEFAULT_SMTP_PORT
    assert config.regime_lookback == 60
    assert config.kelly_fraction == 0.25
    assert config.fred_api_key is None
    assert config.brave_api_key is None


def test_Config_with_secret_fields() -> None:
    config: Config = Config(
        alpaca_service="alpaca-paper",
        alpaca_username="alpaca-api-key",
        fred_api_key="fred-secret",
        brave_api_key="brave-secret",
    )

    assert isinstance(config.fred_api_key, SecretStr)
    assert isinstance(config.brave_api_key, SecretStr)
    assert config.fred_api_key.get_secret_value() == "fred-secret"
    assert config.brave_api_key.get_secret_value() == "brave-secret"


def test_Config_with_assignment_is_frozen() -> None:
    config: Config = Config(alpaca_service="alpaca-paper", alpaca_username="alpaca-api-key")

    with pytest.raises(ValidationError):
        config.llm_model = "mutated"
