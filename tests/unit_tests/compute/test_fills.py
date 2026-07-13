"""Tests for money_pit.compute.fills — status mapping, fill observation building, and outcome derivation."""

import pytest

from money_pit.compute.fills import build_fill_observation
from money_pit.compute.fills import derive_execution_outcome
from money_pit.compute.fills import is_terminal_status
from money_pit.compute.fills import map_order_status
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.fills import FillObservation


_TERMINAL_ALPACA_STATUSES: list[str] = [
    "filled",
    "canceled",
    "expired",
    "done_for_day",
    "rejected",
    "replaced",
    "stopped",
    "suspended",
]

_IN_FLIGHT_ALPACA_STATUSES: list[str] = [
    "new",
    "accepted",
    "accepted_for_bidding",
    "pending_new",
    "pending_cancel",
    "pending_replace",
    "pending_review",
    "calculated",
    "held",
    "partially_filled",
]


@pytest.mark.parametrize("status", _TERMINAL_ALPACA_STATUSES)
def test_is_terminal_status_with_terminal(status: str) -> None:
    assert is_terminal_status(status) is True


@pytest.mark.parametrize("status", _IN_FLIGHT_ALPACA_STATUSES)
def test_is_terminal_status_with_in_flight(status: str) -> None:
    assert is_terminal_status(status) is False


@pytest.mark.parametrize(
    ("status", "filled_qty", "expected_phase"),
    [
        ("filled", None, ExecutionPhase.FILLED),
        ("filled", 8.0, ExecutionPhase.FILLED),
        ("filled", 0.0, ExecutionPhase.FILLED),
        ("rejected", None, ExecutionPhase.REJECTED),
        ("rejected", 5.0, ExecutionPhase.REJECTED),
        ("done_for_day", 3.0, ExecutionPhase.PARTIALLY_FILLED),
        ("partially_filled", 2.0, ExecutionPhase.PARTIALLY_FILLED),
        ("canceled", 1.5, ExecutionPhase.PARTIALLY_FILLED),
        ("accepted", None, ExecutionPhase.SUBMITTED),
        ("new", None, ExecutionPhase.SUBMITTED),
        ("expired", 0.0, ExecutionPhase.SUBMITTED),
    ],
)
def test_map_order_status(status: str, filled_qty: float | None, expected_phase: ExecutionPhase) -> None:
    assert map_order_status(status, filled_qty) is expected_phase


def test_map_order_status_with_zero_filled_qty_boundary() -> None:
    assert map_order_status("canceled", 0.0) is ExecutionPhase.SUBMITTED


def test_build_fill_observation_computes_realized_notional() -> None:
    observation = build_fill_observation("filled", 8.0, 184.5)

    assert isinstance(observation, FillObservation)
    assert observation.realized_notional == 1476.0
    assert observation.phase is ExecutionPhase.FILLED
    assert observation.status == "filled"
    assert observation.filled_qty == 8.0
    assert observation.filled_avg_price == 184.5


@pytest.mark.parametrize(
    ("filled_qty", "filled_avg_price"),
    [
        (None, 184.5),
        (8.0, None),
        (None, None),
    ],
)
def test_build_fill_observation_with_missing_component_leaves_notional_none(
    filled_qty: float | None, filled_avg_price: float | None
) -> None:
    observation = build_fill_observation("new", filled_qty, filled_avg_price)

    assert observation.realized_notional is None


def test_build_fill_observation_carries_raw_status_verbatim() -> None:
    observation = build_fill_observation("accepted_for_bidding", None, None)

    assert observation.status == "accepted_for_bidding"
    assert observation.phase is ExecutionPhase.SUBMITTED


def test_build_fill_observation_phase_matches_map_order_status() -> None:
    observation = build_fill_observation("done_for_day", 3.0, 100.0)

    assert observation.phase is map_order_status("done_for_day", 3.0)


@pytest.mark.parametrize(
    ("phases", "expected_outcome"),
    [
        ([ExecutionPhase.FILLED, ExecutionPhase.FILLED], ExecutionOutcome.EXECUTED_CLEAN),
        ([], ExecutionOutcome.EXECUTED_CLEAN),
        ([ExecutionPhase.FILLED, ExecutionPhase.SUBMITTED], ExecutionOutcome.EXECUTED_INCOMPLETE),
        ([ExecutionPhase.FILLED, ExecutionPhase.PARTIALLY_FILLED], ExecutionOutcome.EXECUTED_INCOMPLETE),
        ([ExecutionPhase.FILLED, ExecutionPhase.REJECTED], ExecutionOutcome.EXECUTION_FAILED),
        ([ExecutionPhase.FILLED, ExecutionPhase.FAILED], ExecutionOutcome.EXECUTION_FAILED),
    ],
)
def test_derive_execution_outcome(
    phases: list[ExecutionPhase], expected_outcome: ExecutionOutcome
) -> None:
    assert derive_execution_outcome(phases) is expected_outcome


@pytest.mark.parametrize(
    "phases",
    [
        [ExecutionPhase.SUBMITTED, ExecutionPhase.REJECTED],
        [ExecutionPhase.PARTIALLY_FILLED, ExecutionPhase.FAILED],
        [ExecutionPhase.SUBMITTED, ExecutionPhase.FAILED, ExecutionPhase.REJECTED],
    ],
)
def test_derive_execution_outcome_incomplete_outranks_failure(phases: list[ExecutionPhase]) -> None:
    assert derive_execution_outcome(phases) is ExecutionOutcome.EXECUTED_INCOMPLETE
