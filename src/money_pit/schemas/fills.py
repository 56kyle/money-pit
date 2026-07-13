"""Module containing the FillObservation model of an observed Alpaca order fill for the money_pit package."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import ExecutionPhase


class FillObservation(BaseModel):
    """A single order's observed fill state, derived from a raw Alpaca order status read."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    phase: ExecutionPhase
    status: str
    filled_qty: float | None
    filled_avg_price: float | None
    realized_notional: float | None
