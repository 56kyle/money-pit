"""Module containing tax-lot boundary contracts for the money_pit package."""

from enum import StrEnum
from typing import ClassVar
from typing import Protocol

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class LotSelectionPolicy(StrEnum):
    """Supported deterministic lot-selection policies."""

    FIFO = "fifo"
    LIFO = "lifo"
    HIGHEST_COST = "highest_cost"
    SPECIFIC_ID = "specific_id"


class TaxLot(BaseModel):
    """A known acquisition lot in a taxable account."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    lot_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    instrument: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    unit_cost: float = Field(ge=0)
    acquired_at: AwareDatetime


class TaxLotSnapshot(BaseModel):
    """Point-in-time known tax lots and completeness declaration."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str = Field(min_length=1)
    captured_at: AwareDatetime
    lots: tuple[TaxLot, ...]
    complete_for_known_accounts: bool
    unknown_external_activity: bool


class TaxLotProvider(Protocol):
    """Boundary for reading point-in-time tax-lot state."""

    def snapshot(self) -> TaxLotSnapshot:
        """Return the currently known immutable lot snapshot."""
        ...
