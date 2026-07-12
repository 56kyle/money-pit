"""Tests for build_execution_params — positive path against the REAL pinned schema and schema-source fail-closed contract.

The happy-path tests validate the emitted payload against the real committed Alpaca order
schema (via stub_free_order_schema_path), so they enforce the true contract: qty/notional as
strings under the schema-literal keys, additionalProperties:false, required [symbol, side].
Small hand-written schemas survive only for the dedicated negative cases.
"""

import json
from pathlib import Path

import jsonschema
import pytest
from pytest import FixtureRequest

from money_pit.compute.execution_params import InvalidExecutionAmountError
from money_pit.compute.execution_params import build_execution_params
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_PATH
from money_pit.mcp.order_schema import AlpacaOrderSchemaMalformedError
from money_pit.mcp.order_schema import AlpacaOrderSchemaMissingError
from money_pit.mcp.order_schema import load_order_schema
from money_pit.schemas.enums import ActionType


@pytest.mark.parametrize(
    ("action_type", "expected_side"),
    [
        (ActionType.BUY, "buy"),
        (ActionType.ADD, "buy"),
        (ActionType.SELL, "sell"),
        (ActionType.TRIM, "sell"),
    ],
)
def test_build_execution_params_side(
    action_type: ActionType, expected_side: str, stub_free_order_schema_path: Path
) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=action_type,
        dollar_amount=1500.0,
        schema_path=stub_free_order_schema_path,
    )

    assert result.side == expected_side


def test_build_execution_params_passes_through_symbol_and_notional(stub_free_order_schema_path: Path) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="MSFT",
        action_type=ActionType.BUY,
        dollar_amount=750.0,
        schema_path=stub_free_order_schema_path,
    )

    assert result.symbol == "MSFT"
    assert result.notional == "750.00"
    assert result.qty is None


def test_build_execution_params_client_order_id(stub_free_order_schema_path: Path) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
        schema_path=stub_free_order_schema_path,
    )

    assert result.client_order_id == "2026-01-01_00-00-00:A001"


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [("type", "market"), ("time_in_force", "day")],
)
def test_build_execution_params_order_constants(
    attribute: str, expected: str, stub_free_order_schema_path: Path
) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
        schema_path=stub_free_order_schema_path,
    )

    assert getattr(result, attribute) == expected


def test_build_execution_params_with_schema_satisfying_emission(
    stub_free_order_schema_path: Path,
) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
        schema_path=stub_free_order_schema_path,
    )

    assert result.symbol == "NVDA"
    assert result.side == "buy"


def test_build_execution_params_emitted_payload_validates_against_real_schema(
    stub_free_order_schema_path: Path,
) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="MSFT",
        action_type=ActionType.BUY,
        dollar_amount=750.0,
        schema_path=stub_free_order_schema_path,
    )
    payload = result.to_order_payload()

    jsonschema.validate(instance=payload, schema=load_order_schema(ALPACA_ORDER_SCHEMA_PATH))
    assert payload["notional"] == "750.00"
    assert isinstance(payload["notional"], str)
    assert "quantity" not in payload
    assert "qty" not in payload


def test_build_execution_params_with_minimum_notional(
    stub_free_order_schema_path: Path,
) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="MSFT",
        action_type=ActionType.BUY,
        dollar_amount=0.01,
        schema_path=stub_free_order_schema_path,
    )
    payload = result.to_order_payload()

    jsonschema.validate(instance=payload, schema=load_order_schema(ALPACA_ORDER_SCHEMA_PATH))
    assert result.notional == "0.01"


@pytest.mark.parametrize(
    "dollar_amount",
    [float("nan"), float("inf"), float("-inf"), 0.0, -100.0, 0.004],
)
def test_build_execution_params_with_invalid_amount(
    dollar_amount: float, stub_free_order_schema_path: Path
) -> None:
    with pytest.raises(InvalidExecutionAmountError):
        _ = build_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="NVDA",
            action_type=ActionType.BUY,
            dollar_amount=dollar_amount,
            schema_path=stub_free_order_schema_path,
        )


def test_build_execution_params_with_invalid_amount_precedes_schema_load(tmp_path: Path) -> None:
    missing_schema: Path = tmp_path / "not_yet_pinned.json"

    with pytest.raises(InvalidExecutionAmountError):
        _ = build_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="NVDA",
            action_type=ActionType.BUY,
            dollar_amount=-100.0,
            schema_path=missing_schema,
        )


@pytest.fixture
def order_schema__required(request: FixtureRequest) -> list[str]:
    return getattr(request, "param", ["symbol", "side", "type", "time_in_force"])


@pytest.fixture
def order_schema(request: FixtureRequest, order_schema__required: list[str]) -> dict[str, object]:
    return getattr(
        request,
        "param",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "symbol": {"type": "string"},
                "notional": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "qty": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "type": {
                    "type": "string",
                    "enum": ["market", "limit", "stop", "stop_limit"],
                },
                "time_in_force": {
                    "type": "string",
                    "enum": ["day", "gtc", "ioc", "fok"],
                },
                "limit_price": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "client_order_id": {"type": "string"},
            },
            "required": order_schema__required,
        },
    )


@pytest.fixture
def order_schema__path(tmp_path: Path, order_schema: dict[str, object]) -> Path:
    path: Path = tmp_path / "alpaca_order_schema.json"
    _ = path.write_text(json.dumps(order_schema), encoding="utf-8")
    return path


@pytest.fixture
def non_object_schema_path(tmp_path: Path) -> Path:
    path: Path = tmp_path / "alpaca_order_schema.json"
    _ = path.write_text("[]", encoding="utf-8")
    return path


@pytest.fixture
def unparseable_schema_path(tmp_path: Path) -> Path:
    path: Path = tmp_path / "alpaca_order_schema.json"
    _ = path.write_text("{ not valid json", encoding="utf-8")
    return path


def test_build_execution_params_with_missing_schema_file(tmp_path: Path) -> None:
    missing_schema: Path = tmp_path / "not_yet_pinned.json"

    with pytest.raises(AlpacaOrderSchemaMissingError):
        _ = build_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="NVDA",
            action_type=ActionType.BUY,
            dollar_amount=1500.0,
            schema_path=missing_schema,
        )


def test_build_execution_params_with_non_object_schema(
    non_object_schema_path: Path,
) -> None:
    with pytest.raises(AlpacaOrderSchemaMalformedError):
        _ = build_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="NVDA",
            action_type=ActionType.BUY,
            dollar_amount=1500.0,
            schema_path=non_object_schema_path,
        )


def test_build_execution_params_with_unparseable_schema(
    unparseable_schema_path: Path,
) -> None:
    with pytest.raises(AlpacaOrderSchemaMalformedError):
        _ = build_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="NVDA",
            action_type=ActionType.BUY,
            dollar_amount=1500.0,
            schema_path=unparseable_schema_path,
        )


@pytest.mark.parametrize(
    "order_schema__required",
    [["symbol", "side", "type", "time_in_force", "limit_price"]],
    indirect=True,
)
def test_build_execution_params_with_over_constrained_schema(
    order_schema__path: Path,
) -> None:
    with pytest.raises(jsonschema.ValidationError):
        _ = build_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="NVDA",
            action_type=ActionType.BUY,
            dollar_amount=1500.0,
            schema_path=order_schema__path,
        )
