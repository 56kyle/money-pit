"""Module responsible for handling config used throughout the money_pit package."""
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

from money_pit.constants import DEFAULT_CONFIG_PATH


class Config(BaseModel):
    """The primary config for the money_pit package."""

    alpaca_service: str
    alpaca_username: str

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
