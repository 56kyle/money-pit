"""Module containing the InstrumentFacts reference-data model for a candidate instrument in the money_pit package."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict


class InstrumentFacts(BaseModel):
    """Best-effort yfinance reference-data view of a candidate instrument being considered for a new position."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    sector: str
    is_etf: bool
    holdings: list[str]
