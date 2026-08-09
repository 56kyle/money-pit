"""Module containing typed fill observation and reconciliation helpers."""

from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict


class ExecutionPhase(StrEnum):
    """Observed lifecycle phase of one submitted broker order."""

    PLANNED = "PLANNED"
    PREFLIGHT_OK = "PREFLIGHT_OK"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    SKIPPED = "SKIPPED"


class FillObservation(BaseModel):
    """One read-only observation of a broker order's fill state."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    phase: ExecutionPhase
    status: str
    filled_qty: float | None
    filled_avg_price: float | None
    realized_notional: float | None


_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"filled", "canceled", "expired", "done_for_day", "rejected", "replaced", "stopped", "suspended"},
)


def is_terminal_status(status: str) -> bool:
    """Return whether a broker status cannot receive further fills."""
    return status in _TERMINAL_STATUSES


def map_order_status(status: str, filled_qty: float | None) -> ExecutionPhase:
    """Map a broker status and quantity to the exact observed phase."""
    if status == "filled":
        return ExecutionPhase.FILLED
    if status == "rejected":
        return ExecutionPhase.REJECTED
    if filled_qty is not None and filled_qty > 0:
        return ExecutionPhase.PARTIALLY_FILLED
    return ExecutionPhase.SUBMITTED


def build_fill_observation(
    status: str,
    filled_qty: float | None,
    filled_avg_price: float | None,
) -> FillObservation:
    """Build one typed observation from a bounded broker response."""
    realized_notional = (
        filled_qty * filled_avg_price if filled_qty is not None and filled_avg_price is not None else None
    )
    return FillObservation(
        phase=map_order_status(status, filled_qty),
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        realized_notional=realized_notional,
    )
