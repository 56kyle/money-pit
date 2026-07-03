"""Position, CorrelatedOverlap, PortfolioSnapshot — portfolio_snapshot.json contract."""
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import FactorTag


class Position(BaseModel):
    """A single open position as reported by the Alpaca account snapshot."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    quantity: float
    cost_basis: float
    current_value: float
    unrealized_pl: float
    sector: str
    factor_tags: list[FactorTag]


class CorrelatedOverlap(BaseModel):
    """A pair or group of tickers that share correlated or duplicative exposure."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    tickers: list[str]
    note: str


class PortfolioSnapshot(BaseModel):
    """Full account state as of a run's start; written to portfolio_snapshot.json."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    as_of: str
    total_account_value: float
    available_cash: float
    positions: list[Position]
    sector_weights: dict[str, float]
    correlated_overlaps: list[CorrelatedOverlap]
