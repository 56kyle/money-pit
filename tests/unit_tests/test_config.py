"""Tests for money_pit.config's credential resolution — the fail-closed keyring + paper/live gates.

Pins resolve_alpaca_credentials' keyring-backed resolution reflecting config.alpaca_paper, and
resolve_gmail_app_password's fail-closed behavior. A real in-memory keyring backend stands in for the OS
keyring (no mocks). Assertions target the CredentialResolutionError TYPE and enum/field values, never text.
"""

from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from pydantic import SecretStr
from pydantic import ValidationError
from pytest import FixtureRequest
from pytest import MonkeyPatch

from money_pit.config import DEFAULT_OWNER_RECIPIENT
from money_pit.config import ENV_PREFIX
from money_pit.config import _BLANK_KEYRING_FIELD_MESSAGE
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.config import _missing_required_env_vars
from money_pit.config import load_config
from money_pit.config import resolve_alpaca_credentials
from money_pit.config import resolve_gmail_app_password
from money_pit.constants import DEFAULT_SMTP_HOST
from money_pit.constants import DEFAULT_SMTP_PORT
from money_pit.constants import GMAIL_KEYRING_SERVICE
from tests.unit_tests.conftest import InMemoryKeyring


_REQUIRED_ENV_WITHOUT_PAPER: str = (
    "MONEY_PIT__ALPACA_SERVICE=alpaca-paper\nMONEY_PIT__ALPACA_USERNAME=alpaca-api-key\n"
)
_REQUIRED_ENV: str = f"{_REQUIRED_ENV_WITHOUT_PAPER}MONEY_PIT__ALPACA_PAPER=true\n"


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
def config__alpaca_paper(request: FixtureRequest) -> bool:
    return getattr(request, "param", True)


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
    config__alpaca_paper: bool,
) -> Config:
    return getattr(
        request,
        "param",
        Config(
            alpaca_service=config__alpaca_service,
            alpaca_username=config__alpaca_username,
            gmail_address=config__gmail_address,
            alpaca_paper=config__alpaca_paper,
        ),
    )


@pytest.mark.parametrize(
    ("config__alpaca_paper", "expected_paper"),
    [(True, True), (False, False)],
    indirect=["config__alpaca_paper"],
)
def test_resolve_alpaca_credentials_with_valid(
    config: Config, secret_key: str, in_memory_keyring: InMemoryKeyring, expected_paper: bool
) -> None:
    in_memory_keyring.set_password(config.alpaca_service, config.alpaca_username, secret_key)

    result: AlpacaCredentials = resolve_alpaca_credentials(config)

    assert result == AlpacaCredentials(
        api_key=config.alpaca_username, secret_key=SecretStr(secret_key), paper=expected_paper
    )


def test_resolve_alpaca_credentials_with_missing_secret(config: Config, in_memory_keyring: InMemoryKeyring) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = resolve_alpaca_credentials(config)


def test_resolve_alpaca_credentials_never_holds_the_secret_as_a_run_lifetime_plaintext_attribute(
    config: Config, secret_key: str, in_memory_keyring: InMemoryKeyring
) -> None:
    """Pin ADR 0032 decision #1: the Alpaca secret lives on AlpacaCredentials as SecretStr, never as a plain str.

    AlpacaCredentials is a default-repr dataclass held for the whole run inside AlpacaWriteDeps, the portfolio
    fetcher, and the fill observer, so a plain str here reaches any repr() or frame-locals traceback of those.
    """
    in_memory_keyring.set_password(config.alpaca_service, config.alpaca_username, secret_key)

    result: AlpacaCredentials = resolve_alpaca_credentials(config)

    assert result.secret_key.get_secret_value() == secret_key
    assert secret_key not in repr(result)
    assert secret_key not in str(vars(result))


def test_resolve_gmail_app_password_with_valid(
    config: Config, gmail_app_password: str, in_memory_keyring: InMemoryKeyring
) -> None:
    assert config.gmail_address is not None
    in_memory_keyring.set_password(config.gmail_service, config.gmail_address, gmail_app_password)

    assert resolve_gmail_app_password(config).get_secret_value() == gmail_app_password


@pytest.mark.parametrize("config__gmail_address", [None], indirect=True)
def test_resolve_gmail_app_password_with_no_address(config: Config, in_memory_keyring: InMemoryKeyring) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = resolve_gmail_app_password(config)


def test_resolve_gmail_app_password_with_missing_secret(config: Config, in_memory_keyring: InMemoryKeyring) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = resolve_gmail_app_password(config)


class KeyringWasConsultedError(Exception):
    """Raised by refusing_keyring to turn any keyring lookup into a test failure rather than a silent pass."""


class _RefusingKeyring(KeyringBackend):
    """A real KeyringBackend that refuses every lookup, so 'fails before touching keyring' is directly observable."""

    priority = 1  # pyright: ignore[reportAssignmentType]

    def get_password(self, service: str, username: str) -> str | None:
        raise KeyringWasConsultedError(f"keyring was consulted for service {service!r}, username {username!r}")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise KeyringWasConsultedError(f"keyring was written for service {service!r}, username {username!r}")

    def delete_password(self, service: str, username: str) -> None:
        raise KeyringWasConsultedError(f"keyring was cleared for service {service!r}, username {username!r}")


@pytest.fixture
def refusing_keyring() -> Iterator[_RefusingKeyring]:
    """Install a keyring backend that raises on any access, restoring the prior backend on teardown."""
    previous: KeyringBackend = keyring.get_keyring()
    backend: _RefusingKeyring = _RefusingKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


_BLANK_VALUES: list[str] = ["", " ", "\t", "   \n  "]


def _blank_field_message(field: str, credential: str) -> str:
    return _BLANK_KEYRING_FIELD_MESSAGE.format(field=field, credential=credential)


_ALPACA_CREDENTIAL_LABEL: str = "Alpaca credentials"
_GMAIL_CREDENTIAL_LABEL: str = "a Gmail app password"


@pytest.mark.parametrize("config__alpaca_service", _BLANK_VALUES, indirect=True)
def test_resolve_alpaca_credentials_with_blank_service(config: Config, refusing_keyring: _RefusingKeyring) -> None:
    """A blank lookup key cannot name a stored secret, so it is refused before the keyring is consulted.

    refusing_keyring is what makes the 'before' load-bearing: were the guard to fall through, the lookup would
    raise KeyringWasConsultedError and this test would error rather than pass on the missing-secret path.
    """
    with pytest.raises(CredentialResolutionError) as exc_info:
        _ = resolve_alpaca_credentials(config)

    assert str(exc_info.value) == _blank_field_message("alpaca_service", _ALPACA_CREDENTIAL_LABEL)


@pytest.mark.parametrize("config__alpaca_username", _BLANK_VALUES, indirect=True)
def test_resolve_alpaca_credentials_with_blank_username(config: Config, refusing_keyring: _RefusingKeyring) -> None:
    with pytest.raises(CredentialResolutionError) as exc_info:
        _ = resolve_alpaca_credentials(config)

    assert str(exc_info.value) == _blank_field_message("alpaca_username", _ALPACA_CREDENTIAL_LABEL)


@pytest.fixture
def config__gmail_service(request: FixtureRequest) -> str:
    return getattr(request, "param", GMAIL_KEYRING_SERVICE)


@pytest.fixture
def config_with_gmail_service(
    config__alpaca_service: str,
    config__alpaca_username: str,
    config__alpaca_paper: bool,
    config__gmail_address: str | None,
    config__gmail_service: str,
) -> Config:
    return Config(
        alpaca_service=config__alpaca_service,
        alpaca_username=config__alpaca_username,
        alpaca_paper=config__alpaca_paper,
        gmail_address=config__gmail_address,
        gmail_service=config__gmail_service,
    )


@pytest.mark.parametrize("config__gmail_service", _BLANK_VALUES, indirect=True)
def test_resolve_gmail_app_password_with_blank_service(
    config_with_gmail_service: Config, refusing_keyring: _RefusingKeyring
) -> None:
    with pytest.raises(CredentialResolutionError) as exc_info:
        _ = resolve_gmail_app_password(config_with_gmail_service)

    assert str(exc_info.value) == _blank_field_message("gmail_service", _GMAIL_CREDENTIAL_LABEL)


@pytest.mark.parametrize("config__gmail_address", _BLANK_VALUES, indirect=True)
def test_resolve_gmail_app_password_with_blank_address(
    config_with_gmail_service: Config, refusing_keyring: _RefusingKeyring
) -> None:
    """A blank address is distinct from an unset one: it is configuration that looks present and is not."""
    with pytest.raises(CredentialResolutionError) as exc_info:
        _ = resolve_gmail_app_password(config_with_gmail_service)

    assert str(exc_info.value) == _blank_field_message("gmail_address", _GMAIL_CREDENTIAL_LABEL)


@pytest.mark.parametrize("config__gmail_address", [None], indirect=True)
def test_resolve_gmail_app_password_with_no_address_keeps_its_own_message(
    config: Config, refusing_keyring: _RefusingKeyring
) -> None:
    """The pre-existing unset-address refusal is unchanged by the blank-field guards added alongside it.

    An operator who never configured Gmail and one who configured it to whitespace need different messages,
    so the blank-field phrasing must not have absorbed the None case.
    """
    with pytest.raises(CredentialResolutionError) as exc_info:
        _ = resolve_gmail_app_password(config)

    assert str(exc_info.value) != _blank_field_message("gmail_address", _GMAIL_CREDENTIAL_LABEL)
    assert "gmail_address" in str(exc_info.value)


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

    assert missing == ["MONEY_PIT__ALPACA_SERVICE", "MONEY_PIT__ALPACA_USERNAME", "MONEY_PIT__ALPACA_PAPER"]


@pytest.mark.parametrize("env_file__content", ["MONEY_PIT__ALPACA_SERVICE=alpaca-paper\n"], indirect=True)
def test_load_config_with_missing_required_var(env_file: Path) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = load_config(env_file)


@pytest.mark.parametrize(
    "env_file__content",
    [f"{_REQUIRED_ENV}MONEY_PIT__SMTP_PORT=not-a-port\n"],
    indirect=True,
)
def test_load_config_with_mistyped_var(env_file: Path) -> None:
    with pytest.raises(ValidationError):
        _ = load_config(env_file)


@pytest.mark.parametrize("env_file__content", [_REQUIRED_ENV_WITHOUT_PAPER], indirect=True)
def test_load_config_with_alpaca_paper_unset(env_file: Path) -> None:
    with pytest.raises(CredentialResolutionError) as exc_info:
        _ = load_config(env_file)

    assert f"{ENV_PREFIX}ALPACA_PAPER" in str(exc_info.value)


@pytest.mark.parametrize("env_file__content", [f"{_REQUIRED_ENV_WITHOUT_PAPER}MONEY_PIT__ALPACA_PAPER=notabool\n"], indirect=True)
def test_load_config_with_alpaca_paper_mistyped(env_file: Path) -> None:
    with pytest.raises(ValidationError):
        _ = load_config(env_file)


def test_load_config_with_env_override(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MONEY_PIT__ALPACA_SERVICE", "alpaca-paper")
    monkeypatch.setenv("MONEY_PIT__ALPACA_USERNAME", "alpaca-api-key")
    monkeypatch.setenv("MONEY_PIT__ALPACA_PAPER", "true")
    monkeypatch.setenv("MONEY_PIT__LLM_MODEL", "override-model")

    config: Config = load_config(tmp_path / "absent.env")

    assert config.llm_model == "override-model"


@pytest.mark.parametrize(
    "env_file__content",
    [f"{_REQUIRED_ENV}MONEY_PIT__CURRENT_EVENTS_LOOKBACK_DAYS=21\n"],
    indirect=True,
)
def test_load_config_with_current_events_lookback_days_override(env_file: Path) -> None:
    config: Config = load_config(env_file)

    assert config.current_events_lookback_days == 21


@pytest.mark.parametrize(
    "env_file__content",
    [f"{_REQUIRED_ENV}MONEY_PIT__CURRENT_EVENTS_LOOKBACK_DAYS=-1\n"],
    indirect=True,
)
def test_load_config_with_negative_current_events_lookback_days(env_file: Path) -> None:
    """A negative lookback moves the evidence cutoff forward of publication, so it must be rejected at load."""
    with pytest.raises(ValidationError):
        _ = load_config(env_file)


@pytest.mark.parametrize(
    ("field_name", "expected_default"),
    [
        ("llm_model", "gpt-5"),
        ("gmail_address", None),
        ("gmail_service", GMAIL_KEYRING_SERVICE),
        ("owner_recipient", DEFAULT_OWNER_RECIPIENT),
        ("smtp_host", DEFAULT_SMTP_HOST),
        ("smtp_port", DEFAULT_SMTP_PORT),
        ("regime_lookback", 60),
        ("current_events_lookback_days", 7),
        ("kelly_fraction", 0.25),
        ("fred_api_key", None),
        ("brave_api_key", None),
    ],
)
def test_config_with_defaults(field_name: str, expected_default: object) -> None:
    config: Config = Config(alpaca_service="alpaca-paper", alpaca_username="alpaca-api-key", alpaca_paper=True)

    assert getattr(config, field_name) == expected_default


def test_config_with_secret_fields() -> None:
    config: Config = Config(
        alpaca_service="alpaca-paper",
        alpaca_username="alpaca-api-key",
        alpaca_paper=True,
        fred_api_key="fred-secret",
        brave_api_key="brave-secret",
    )

    assert isinstance(config.fred_api_key, SecretStr)
    assert isinstance(config.brave_api_key, SecretStr)
    assert config.fred_api_key.get_secret_value() == "fred-secret"
    assert config.brave_api_key.get_secret_value() == "brave-secret"


def test_config_with_assignment_is_frozen() -> None:
    config: Config = Config(alpaca_service="alpaca-paper", alpaca_username="alpaca-api-key", alpaca_paper=True)

    with pytest.raises(ValidationError):
        config.llm_model = "mutated"
