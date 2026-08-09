"""Module containing typed instrument classification contracts."""

from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict


class InstrumentFacts(BaseModel):
    """Best-effort yfinance reference-data view of a candidate instrument being considered for a new position."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    sector: str
    is_etf: bool
    holdings: list[str]


class InstrumentExposureClass(StrEnum):
    """Deterministic exposure category used by portfolio constraints."""

    SINGLE_STOCK = "single_stock"
    BROAD_MARKET_EQUITY_ETF = "broad_market_equity_etf"
    THEMATIC_EQUITY_ETF = "thematic_equity_etf"
    FIXED_INCOME_ETF = "fixed_income_etf"
    CASH_EQUIVALENT_ETF = "cash_equivalent_etf"
