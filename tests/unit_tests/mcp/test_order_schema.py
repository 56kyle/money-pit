"""Tests for load_order_schema — the fail-closed pinned-schema gate and its precedence.

The committed alpaca_order_schema.json is now the real, pinned schema (no x_stub sentinel),
so the default path loads successfully. The unpinned-stub GUARD is still pinned independently
by writing an explicit sentinel-carrying stub to a temp path and asserting it fails closed,
so a future accidental un-pin remains caught. Assertions target exception TYPES, not message text.
"""

import json
from pathlib import Path

import pytest

from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_PATH
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL
from money_pit.mcp.order_schema import AlpacaOrderSchemaMalformedError
from money_pit.mcp.order_schema import AlpacaOrderSchemaMissingError
from money_pit.mcp.order_schema import AlpacaOrderSchemaNotPinnedError
from money_pit.mcp.order_schema import load_order_schema


@pytest.fixture
def falsy_sentinel_schema_path(tmp_path: Path) -> Path:
    schema: dict[str, object] = json.loads(ALPACA_ORDER_SCHEMA_PATH.read_text(encoding="utf-8"))
    schema[ALPACA_ORDER_SCHEMA_STUB_SENTINEL] = False
    path: Path = tmp_path / "alpaca_order_schema.json"
    _ = path.write_text(json.dumps(schema), encoding="utf-8")
    return path


@pytest.fixture
def unpinned_stub_schema_path(tmp_path: Path) -> Path:
    schema: dict[str, object] = json.loads(ALPACA_ORDER_SCHEMA_PATH.read_text(encoding="utf-8"))
    schema[ALPACA_ORDER_SCHEMA_STUB_SENTINEL] = True
    path: Path = tmp_path / "alpaca_order_schema.json"
    _ = path.write_text(json.dumps(schema), encoding="utf-8")
    return path


def test_load_order_schema_with_unpinned_stub(unpinned_stub_schema_path: Path) -> None:
    with pytest.raises(AlpacaOrderSchemaNotPinnedError):
        _ = load_order_schema(unpinned_stub_schema_path)


def test_load_order_schema_with_pinned_schema() -> None:
    result = load_order_schema(ALPACA_ORDER_SCHEMA_PATH)

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


def test_load_order_schema_default_path_is_pinned() -> None:
    schema = load_order_schema()

    assert isinstance(schema, dict)
    assert ALPACA_ORDER_SCHEMA_STUB_SENTINEL not in schema
