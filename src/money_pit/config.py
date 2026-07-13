"""Module responsible for handling config used throughout the money_pit package."""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import keyring
from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic import ValidationError
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

from money_pit.constants import DEFAULT_SMTP_HOST
from money_pit.constants import DEFAULT_SMTP_PORT
from money_pit.constants import GMAIL_KEYRING_SERVICE
from money_pit.constants import default_config_path


ENV_PREFIX: str = "MONEY_PIT__"
_DEFAULT_LLM_MODEL: str = "claude-sonnet-5"

DEFAULT_OWNER_RECIPIENT: str = "56kyleoliver@gmail.com"


class CredentialResolutionError(Exception):
    """Raised when a required secret or required credential config cannot be resolved."""


@dataclass(frozen=True)
class AlpacaCredentials:
    """Resolved Alpaca API credentials plus the paper/live routing decision."""

    api_key: str
    secret_key: str
    paper: bool


class Config(BaseSettings):
    """The primary config for the money_pit package."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(env_prefix=ENV_PREFIX, frozen=True)

    alpaca_service: str
    alpaca_username: str
    alpaca_paper: bool

    gmail_address: str | None = None
    gmail_service: str = GMAIL_KEYRING_SERVICE
    owner_recipient: str = DEFAULT_OWNER_RECIPIENT
    smtp_host: str = DEFAULT_SMTP_HOST
    smtp_port: int = DEFAULT_SMTP_PORT

    regime_lookback: int = 60
    regime_band: float = 0.5
    kelly_fraction: float = 0.25
    max_position_weight: float = 0.10
    haircut_unverified: float = 0.5
    haircut_uncertain: float = 0.75
    ev_gate: float = 0.03
    sector_cap: float = 0.25
    cash_min: float = 0.05
    overlap_limit: float = 0.30
    execution_fill_poll_interval_seconds: float = 1.0
    execution_fill_poll_timeout_seconds: float = 30.0
    threshold_yield_curve: float = 0.0
    threshold_credit_spreads: float = 3.0
    threshold_pmi: float = 50.0
    threshold_earnings_revisions: float = 0.0
    threshold_inflation: float = 2.5
    llm_model: str = _DEFAULT_LLM_MODEL
    fred_api_key: SecretStr | None = None
    brave_api_key: SecretStr | None = None


def _missing_required_env_vars(error: ValidationError) -> list[str]:
    """Return the MONEY_PIT__ environment variable names for the missing-required fields in a Config error."""
    return [
        f"{ENV_PREFIX}{str(entry['loc'][0]).upper()}"
        for entry in error.errors()
        if entry["type"] == "missing" and entry["loc"]
    ]


def load_config(path: Path | None = None) -> Config:
    """Load a fresh, frozen Config, raising CredentialResolutionError on a missing required var and propagating pydantic.ValidationError on a mistyped one."""
    resolved_path: Path = path or default_config_path()
    _ = load_dotenv(resolved_path)
    try:
        return Config()
    except ValidationError as error:
        missing: list[str] = _missing_required_env_vars(error)
        if not missing:
            raise
        raise CredentialResolutionError(
            f"Missing required money_pit config from the environment: {', '.join(missing)}."
        ) from error


def resolve_alpaca_credentials(config: Config) -> AlpacaCredentials:
    """Resolve Alpaca credentials from config plus keyring, failing closed on a missing secret."""
    secret_key: str | None = keyring.get_password(config.alpaca_service, config.alpaca_username)
    if secret_key is None:
        raise CredentialResolutionError(
            f"No Alpaca secret in keyring for service {config.alpaca_service!r}, username {config.alpaca_username!r}."
        )
    return AlpacaCredentials(
        api_key=config.alpaca_username,
        secret_key=secret_key,
        paper=config.alpaca_paper,
    )


def resolve_gmail_app_password(config: Config) -> str:
    """Resolve the Gmail app password from keyring, failing closed if the address or stored secret is unset."""
    if config.gmail_address is None:
        raise CredentialResolutionError("No gmail_address configured; cannot resolve a Gmail app password.")
    app_password: str | None = keyring.get_password(config.gmail_service, config.gmail_address)
    if app_password is None:
        raise CredentialResolutionError(
            f"No Gmail app password in keyring for service {config.gmail_service!r}, username {config.gmail_address!r}."
        )
    return app_password
