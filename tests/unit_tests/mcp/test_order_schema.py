"""Tests for load_order_schema — the fail-closed pinned-schema gate and its precedence.

Pins the re-enforced unpinned-stub gate: the committed alpaca_order_schema.json carries
the x_stub sentinel, so the default path fails closed with AlpacaOrderSchemaNotPinnedError
until the real schema is pinned. Assertions target exception TYPES, not message text.
"""

import json
from pathlib import Path

import pytest

from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL
from money_pit.mcp.order_schema import AlpacaOrderSchemaMalformedError
from money_pit.mcp.order_schema import AlpacaOrderSchemaMissingError
from money_pit.mcp.order_schema import AlpacaOrderSchemaNotPinnedError
from money_pit.mcp.order_schema import load_order_schema


@pytest.fixture
def falsy_sentinel_schema_path(tmp_path: Path, stub_free_order_schema_path: Path) -> Path:
    schema: dict[str, object] = json.loads(stub_free_order_schema_path.read_text(encoding="utf-8"))
    schema[ALPACA_ORDER_SCHEMA_STUB_SENTINEL] = False
    path: Path = tmp_path / "alpaca_order_schema.json"
    _ = path.write_text(json.dumps(schema), encoding="utf-8")
    return path


def test_load_order_schema_with_unpinned_stub() -> None:
    with pytest.raises(AlpacaOrderSchemaNotPinnedError):
        _ = load_order_schema()


def test_load_order_schema_with_pinned_schema(stub_free_order_schema_path: Path) -> None:
    result = load_order_schema(stub_free_order_schema_path)

    assert isinstance(result, dict)


def test_load_order_schema_with_falsy_sentinel(falsy_sentinel_schema_path: Path) -> None:
    result = load_order_schema(falsy_sentinel_schema_path)

    assert isinstance(result, dict)


def test_load_order_schema_with_missing_file(tmp_path: Path) -> None:
    missing_schema: Path = tmp_path / "not_yet_pinned.json"

    with pytest.raises(AlpacaOrderSchemaMissingError):
        _ = load_order_schema(missing_schema)


def test_load_order_schema_with_unparseable_file(tmp_path: Path) -> None:
    path: Path = tmp_path / "alpaca_order_schema.json"
    _ = path.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(AlpacaOrderSchemaMalformedError):
        _ = load_order_schema(path)


def test_load_order_schema_with_non_object(tmp_path: Path) -> None:
    path: Path = tmp_path / "alpaca_order_schema.json"
    _ = path.write_text("[]", encoding="utf-8")

    with pytest.raises(AlpacaOrderSchemaMalformedError):
        _ = load_order_schema(path)


@pytest.mark.xfail(
    raises=AlpacaOrderSchemaNotPinnedError,
    strict=True,
    reason="committed alpaca_order_schema.json is still the unpinned stub;"
    " pinning the real schema (removing the x_stub sentinel) flips this XPASS->failure,"
    " signalling the stub scaffolding must be removed",
)
def test_load_order_schema_default_path_is_pinned() -> None:
    # Self-removing tripwire; see the xfail reason above for what an XPASS signals.
    schema = load_order_schema()

    assert isinstance(schema, dict)
