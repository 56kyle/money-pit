"""Tests for money_pit.schemas.action_steps.ExecutionParameters side tightening (wave S8, T7).

Pins the loose-str -> constrained tightening of ExecutionParameters.side: only "buy"/"sell"
are accepted, and any other string is rejected at construction. The rejection is red today
because the field is an unconstrained `str`.
"""
import pytest
from pydantic import ValidationError

from money_pit.schemas.action_steps import ExecutionParameters


def _execution_parameters(side: object) -> ExecutionParameters:
    return ExecutionParameters(
        symbol="AAPL",
        notional=None,
        quantity=1.0,
        side=side,  # pyright: ignore[reportArgumentType]
        type="market",
        time_in_force="day",
        client_order_id="oid-1",
    )


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_execution_parameters_with_valid_side(side: str) -> None:
    params = _execution_parameters(side)

    assert params.side == side


def test_execution_parameters_with_invalid_side_raises() -> None:
    with pytest.raises(ValidationError):
        _execution_parameters("notaside")
