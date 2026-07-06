"""Tests for build_execution_params — positive path and schema-source fail-closed contract."""

import json
from pathlib import Path

import jsonschema
import pytest
from pytest import FixtureRequest

from money_pit.compute.execution_params import build_execution_params
from money_pit.mcp.order_schema import AlpacaOrderSchemaMalformedError
from money_pit.mcp.order_schema import AlpacaOrderSchemaMissingError
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
    action_type: ActionType, expected_side: str, order_schema__path: Path
) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=action_type,
        dollar_amount=1500.0,
        schema_path=order_schema__path,
    )

    assert result.side == expected_side


def test_build_execution_params_passes_through_symbol_and_notional(order_schema__path: Path) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="MSFT",
        action_type=ActionType.BUY,
        dollar_amount=750.0,
        schema_path=order_schema__path,
    )

    assert result.symbol == "MSFT"
    assert result.notional == pytest.approx(750.0)


def test_build_execution_params_client_order_id(order_schema__path: Path) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
        schema_path=order_schema__path,
    )

    assert result.client_order_id == "2026-01-01_00-00-00:A001"


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [("type", "market"), ("time_in_force", "day")],
)
def test_build_execution_params_order_constants(
    attribute: str, expected: str, order_schema__path: Path
) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
        schema_path=order_schema__path,
    )

    assert getattr(result, attribute) == expected


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
            "properties": {
                "symbol": {"type": "string"},
                "notional": {"type": "number"},
                "quantity": {"type": "number"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "type": {
                    "type": "string",
                    "enum": ["market", "limit", "stop", "stop_limit"],
                },
                "time_in_force": {
                    "type": "string",
                    "enum": ["day", "gtc", "ioc", "fok"],
                },
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


def test_build_execution_params_with_schema_satisfying_emission(
    order_schema__path: Path,
) -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
        schema_path=order_schema__path,
    )

    assert result.symbol == "NVDA"
    assert result.side == "buy"


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
