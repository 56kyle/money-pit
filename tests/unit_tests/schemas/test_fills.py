"""Tests for execution-owned fill observation contracts."""

import pytest
from pydantic import ValidationError

from money_pit.execution_control.fills import ExecutionPhase
from money_pit.execution_control.fills import FillObservation


def test_fill_observation_with_valid() -> None:
    observation = FillObservation(
        phase=ExecutionPhase.FILLED,
        status="filled",
        filled_qty=8.0,
        filled_avg_price=184.5,
        realized_notional=1476.0,
    )

    assert observation.phase is ExecutionPhase.FILLED
    assert observation.status == "filled"
    assert observation.filled_qty == 8.0
    assert observation.filled_avg_price == 184.5
    assert observation.realized_notional == 1476.0


def test_fill_observation_is_frozen() -> None:
    observation = FillObservation(
        phase=ExecutionPhase.SUBMITTED,
        status="new",
        filled_qty=None,
        filled_avg_price=None,
        realized_notional=None,
    )

    with pytest.raises(ValidationError):
        observation.status = "filled"  # type: ignore[misc]
