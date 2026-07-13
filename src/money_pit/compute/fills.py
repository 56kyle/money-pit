"""Module containing pure Alpaca order-status mapping and run-outcome derivation for the money_pit package.

derive_execution_outcome precedence: an open or partially filled leg outranks a clean
rejection because it leaves the portfolio in an unchosen state that no compensation was
asked to resolve, whereas a rejection is a decision that simply did not take effect.
"""

from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.fills import FillObservation


_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "filled",
        "canceled",
        "expired",
        "done_for_day",
        "rejected",
        "replaced",
        "stopped",
        "suspended",
    }
)

_INCOMPLETE_PHASES: frozenset[ExecutionPhase] = frozenset(
    {ExecutionPhase.SUBMITTED, ExecutionPhase.PARTIALLY_FILLED}
)
_FAILURE_PHASES: frozenset[ExecutionPhase] = frozenset(
    {ExecutionPhase.FAILED, ExecutionPhase.REJECTED}
)


def is_terminal_status(status: str) -> bool:
    return status in _TERMINAL_STATUSES


def map_order_status(status: str, filled_qty: float | None) -> ExecutionPhase:
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
    realized_notional: float | None = (
        filled_qty * filled_avg_price
        if filled_qty is not None and filled_avg_price is not None
        else None
    )
    return FillObservation(
        phase=map_order_status(status, filled_qty),
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        realized_notional=realized_notional,
    )


def derive_execution_outcome(phases: list[ExecutionPhase]) -> ExecutionOutcome:
    phase_set: set[ExecutionPhase] = set(phases)
    if phase_set & _INCOMPLETE_PHASES:
        return ExecutionOutcome.EXECUTED_INCOMPLETE
    if phase_set & _FAILURE_PHASES:
        return ExecutionOutcome.EXECUTION_FAILED
    return ExecutionOutcome.EXECUTED_CLEAN
