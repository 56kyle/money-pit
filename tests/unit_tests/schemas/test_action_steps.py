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
        qty="1",
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


def test_execution_parameters_with_notional_only() -> None:
    params = ExecutionParameters(
        symbol="NVDA",
        notional="10.00",
        qty=None,
        side="buy",
        type="market",
        time_in_force="day",
        client_order_id="oid-1",
    )

    assert params.notional == "10.00"
    assert params.qty is None


def test_execution_parameters_with_qty_only() -> None:
    params = ExecutionParameters(
        symbol="NVDA",
        notional=None,
        qty="5",
        side="buy",
        type="market",
        time_in_force="day",
        client_order_id="oid-1",
    )

    assert params.qty == "5"
    assert params.notional is None


def test_execution_parameters_with_neither_amount_raises() -> None:
    with pytest.raises(ValidationError):
        _ = ExecutionParameters(
            symbol="NVDA",
            notional=None,
            qty=None,
            side="buy",
            type="market",
            time_in_force="day",
            client_order_id="oid-1",
        )


def test_execution_parameters_with_both_amounts_raises() -> None:
    with pytest.raises(ValidationError):
        _ = ExecutionParameters(
            symbol="NVDA",
            notional="10.00",
            qty="5",
            side="buy",
            type="market",
            time_in_force="day",
            client_order_id="oid-1",
        )


def test_execution_parameters_to_order_payload_emits_stored_strings_under_schema_keys() -> None:
    params = ExecutionParameters(
        symbol="NVDA",
        notional="1500.00",
        qty=None,
        side="buy",
        type="market",
        time_in_force="day",
        client_order_id="2026-01-01_00-00-00:A001",
    )

    payload = params.to_order_payload()

    assert payload["notional"] == "1500.00"
    assert "qty" not in payload
    assert "quantity" not in payload


def test_execution_parameters_to_order_payload_with_qty_emits_qty_key_omitting_notional() -> None:
    params = ExecutionParameters(
        symbol="NVDA",
        notional=None,
        qty="10",
        side="buy",
        type="market",
        time_in_force="day",
        client_order_id="2026-01-01_00-00-00:A001",
    )

    payload = params.to_order_payload()

    assert payload["qty"] == "10"
    assert "notional" not in payload
    assert "quantity" not in payload
