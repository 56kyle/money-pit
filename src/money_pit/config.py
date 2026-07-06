"""Module responsible for handling config used throughout the money_pit package."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import keyring
from dotenv import load_dotenv
from pydantic import BaseModel

from money_pit.constants import DEFAULT_CONFIG_PATH
from money_pit.constants import GMAIL_KEYRING_SERVICE


_ALPACA_PAPER_SUFFIX: str = "-paper"
_ALPACA_LIVE_SUFFIX: str = "-live"

DEFAULT_OWNER_RECIPIENT: str = "56kyleoliver@gmail.com"
_DEFAULT_SMTP_HOST: str = "smtp.gmail.com"
_DEFAULT_SMTP_PORT: int = 587


class CredentialResolutionError(Exception):
    """Raised when a required secret cannot be resolved, or a service name is ambiguous about paper vs live."""


@dataclass(frozen=True)
class AlpacaCredentials:
    """Resolved Alpaca API credentials plus the paper/live routing decision."""

    api_key: str
    secret_key: str
    paper: bool


class Config(BaseModel):
    """The primary config for the money_pit package."""

    alpaca_service: str
    alpaca_username: str

    gmail_address: str | None = None
    gmail_service: str = GMAIL_KEYRING_SERVICE
    owner_recipient: str = DEFAULT_OWNER_RECIPIENT
    smtp_host: str = _DEFAULT_SMTP_HOST
    smtp_port: int = _DEFAULT_SMTP_PORT

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
    threshold_yield_curve: float = 0.0
    threshold_credit_spreads: float = 3.0
    threshold_pmi: float = 50.0
    threshold_earnings_revisions: float = 0.0
    threshold_inflation: float = 2.5
    llm_model: str = "claude-sonnet-4-6"
    fred_api_key: str | None = None
    brave_api_key: str | None = None


@lru_cache
def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load the config for the money_pit package."""
    _ = load_dotenv(path)
    alpaca_service: str | None = os.environ.get("MONEY_PIT__ALPACA_SERVICE", None)

    if alpaca_service is None:
        raise ValueError("Failed to get alpaca_service from environment variable MONEY_PIT__ALPACA_SERVICE.")

    alpaca_username: str | None = os.environ.get("MONEY_PIT__ALPACA_USERNAME", None)
    if alpaca_username is None:
        raise ValueError("Failed to get alpaca_username from environment variable MONEY_PIT__ALPACA_USERNAME.")

    return Config(
        alpaca_service=alpaca_service,
        alpaca_username=alpaca_username,
        gmail_address=os.environ.get("MONEY_PIT__GMAIL_ADDRESS", None),
        gmail_service=os.environ.get("MONEY_PIT__GMAIL_SERVICE", GMAIL_KEYRING_SERVICE),
        owner_recipient=os.environ.get("MONEY_PIT__OWNER_RECIPIENT", DEFAULT_OWNER_RECIPIENT),
        smtp_host=os.environ.get("MONEY_PIT__SMTP_HOST", _DEFAULT_SMTP_HOST),
        smtp_port=int(os.environ.get("MONEY_PIT__SMTP_PORT", _DEFAULT_SMTP_PORT)),
        regime_lookback=int(os.environ.get("MONEY_PIT__REGIME_LOOKBACK", 60)),
        regime_band=float(os.environ.get("MONEY_PIT__REGIME_BAND", 0.5)),
        kelly_fraction=float(os.environ.get("MONEY_PIT__KELLY_FRACTION", 0.25)),
        max_position_weight=float(os.environ.get("MONEY_PIT__MAX_POSITION_WEIGHT", 0.10)),
        haircut_unverified=float(os.environ.get("MONEY_PIT__HAIRCUT_UNVERIFIED", 0.5)),
        haircut_uncertain=float(os.environ.get("MONEY_PIT__HAIRCUT_UNCERTAIN", 0.75)),
        ev_gate=float(os.environ.get("MONEY_PIT__EV_GATE", 0.03)),
        sector_cap=float(os.environ.get("MONEY_PIT__SECTOR_CAP", 0.25)),
        cash_min=float(os.environ.get("MONEY_PIT__CASH_MIN", 0.05)),
        overlap_limit=float(os.environ.get("MONEY_PIT__OVERLAP_LIMIT", 0.30)),
        threshold_yield_curve=float(os.environ.get("MONEY_PIT__THRESHOLD_YIELD_CURVE", 0.0)),
        threshold_credit_spreads=float(os.environ.get("MONEY_PIT__THRESHOLD_CREDIT_SPREADS", 3.0)),
        threshold_pmi=float(os.environ.get("MONEY_PIT__THRESHOLD_PMI", 50.0)),
        threshold_earnings_revisions=float(os.environ.get("MONEY_PIT__THRESHOLD_EARNINGS_REVISIONS", 0.0)),
        threshold_inflation=float(os.environ.get("MONEY_PIT__THRESHOLD_INFLATION", 2.5)),
        llm_model=os.environ.get("MONEY_PIT__LLM_MODEL", "claude-sonnet-4-6"),
        fred_api_key=os.environ.get("MONEY_PIT__FRED_API_KEY", None),
        brave_api_key=os.environ.get("MONEY_PIT__BRAVE_API_KEY", None),
    )


def _paper_from_service(service: str) -> bool:
    """Return the paper/live routing decision from the service suffix, never guessing when the suffix is absent."""
    if service.endswith(_ALPACA_PAPER_SUFFIX):
        return True
    if service.endswith(_ALPACA_LIVE_SUFFIX):
        return False
    raise CredentialResolutionError(
        f"Alpaca service {service!r} does not end in {_ALPACA_PAPER_SUFFIX!r} or {_ALPACA_LIVE_SUFFIX!r};"
        + " refusing to guess paper vs live."
    )


def resolve_alpaca_credentials(config: Config) -> AlpacaCredentials:
    """Resolve Alpaca credentials from config plus keyring, failing closed on a missing secret or ambiguous routing."""
    secret_key: str | None = keyring.get_password(config.alpaca_service, config.alpaca_username)
    if secret_key is None:
        raise CredentialResolutionError(
            f"No Alpaca secret in keyring for service {config.alpaca_service!r}, username {config.alpaca_username!r}."
        )
    return AlpacaCredentials(
        api_key=config.alpaca_username,
        secret_key=secret_key,
        paper=_paper_from_service(config.alpaca_service),
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
