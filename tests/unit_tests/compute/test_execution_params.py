"""Tests for build_execution_params — positive path against the REAL pinned schema and its guard ordering.

The happy-path tests validate the emitted payload against the real committed Alpaca order schema
(loaded via the pinned default), so they enforce the true contract: qty/notional as strings under the
schema-literal keys, additionalProperties:false, required [symbol, side]. The schema-source fail-closed
cases (missing / malformed / unpinned-stub) are pinned at the loader in test_order_schema.py now that
build_execution_params no longer takes a schema_path seam and always loads the pinned default.
"""

import jsonschema
import pytest

from money_pit.compute.execution_params import InvalidExecutionAmountError
from money_pit.compute.execution_params import build_execution_params
from money_pit.compute.execution_params import build_quantity_execution_params
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_PATH
from money_pit.mcp.order_schema import load_order_schema
from money_pit.schemas.enums import ActionType


_LOAD_ORDER_SCHEMA_TARGET = "money_pit.compute.execution_params.load_order_schema"


@pytest.mark.parametrize(
    ("action_type", "expected_side"),
    [
        (ActionType.BUY, "buy"),
        (ActionType.ADD, "buy"),
        (ActionType.SELL, "sell"),
        (ActionType.TRIM, "sell"),
    ],
)
def test_build_execution_params_side(action_type: ActionType, expected_side: str) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=action_type,
        dollar_amount=1500.0,
    )

    assert result.side == expected_side


def test_build_execution_params_passes_through_symbol_and_notional() -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="MSFT",
        action_type=ActionType.BUY,
        dollar_amount=750.0,
    )

    assert result.symbol == "MSFT"
    assert result.notional == "750.00"
    assert result.qty is None


def test_build_execution_params_client_order_id() -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
    )

    assert result.client_order_id == "2026-01-01_00-00-00:A001"


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [("type", "market"), ("time_in_force", "day")],
)
def test_build_execution_params_order_constants(attribute: str, expected: str) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
    )

    assert getattr(result, attribute) == expected


def test_build_execution_params_with_schema_satisfying_emission() -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
    )

    assert result.symbol == "NVDA"
    assert result.side == "buy"


def test_build_execution_params_emitted_payload_validates_against_real_schema() -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="MSFT",
        action_type=ActionType.BUY,
        dollar_amount=750.0,
    )
    payload = result.to_order_payload()

    jsonschema.validate(instance=payload, schema=load_order_schema(ALPACA_ORDER_SCHEMA_PATH))
    assert payload["notional"] == "750.00"
    assert isinstance(payload["notional"], str)
    assert "quantity" not in payload
    assert "qty" not in payload


def test_build_execution_params_with_minimum_notional() -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="MSFT",
        action_type=ActionType.BUY,
        dollar_amount=0.01,
    )
    payload = result.to_order_payload()

    jsonschema.validate(instance=payload, schema=load_order_schema(ALPACA_ORDER_SCHEMA_PATH))
    assert result.notional == "0.01"


@pytest.mark.parametrize(
    "dollar_amount",
    [float("nan"), float("inf"), float("-inf"), 0.0, -100.0, 0.004],
)
def test_build_execution_params_with_invalid_amount(dollar_amount: float) -> None:
    with pytest.raises(InvalidExecutionAmountError):
        _ = build_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="NVDA",
            action_type=ActionType.BUY,
            dollar_amount=dollar_amount,
        )


def test_build_execution_params_with_invalid_amount_precedes_schema_load(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("schema load must not be reached when the amount guard rejects")

    monkeypatch.setattr(_LOAD_ORDER_SCHEMA_TARGET, _fail)

    with pytest.raises(InvalidExecutionAmountError):
        _ = build_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="NVDA",
            action_type=ActionType.BUY,
            dollar_amount=-100.0,
        )


def test_build_quantity_execution_params_with_sell() -> None:
    result = build_quantity_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.SELL,
        quantity=10.0,
    )
    payload = result.to_order_payload()

    assert result.qty == "10"
    assert result.notional is None
    assert result.side == "sell"
    assert result.type == "market"
    assert result.time_in_force == "day"
    assert result.client_order_id == "2026-01-01_00-00-00:A001"
    assert "qty" in payload
    assert "notional" not in payload


@pytest.mark.parametrize(
    ("quantity", "expected_qty"),
    [(10.0, "10"), (3.5, "3.5"), (0.001, "0.001"), (10.25, "10.25")],
)
def test_build_quantity_execution_params_formats_quantity(quantity: float, expected_qty: str) -> None:
    result = build_quantity_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.SELL,
        quantity=quantity,
    )

    assert result.qty == expected_qty


def test_build_quantity_execution_params_emitted_payload_validates_against_real_schema() -> None:
    result = build_quantity_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="MSFT",
        action_type=ActionType.SELL,
        quantity=5.0,
    )
    payload = result.to_order_payload()

    jsonschema.validate(instance=payload, schema=load_order_schema(ALPACA_ORDER_SCHEMA_PATH))
    assert payload["qty"] == "5"
    assert "notional" not in payload
    assert "quantity" not in payload


@pytest.mark.parametrize(
    "quantity",
    [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
)
def test_build_quantity_execution_params_with_invalid_quantity(quantity: float) -> None:
    with pytest.raises(InvalidExecutionAmountError):
        _ = build_quantity_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="NVDA",
            action_type=ActionType.SELL,
            quantity=quantity,
        )
