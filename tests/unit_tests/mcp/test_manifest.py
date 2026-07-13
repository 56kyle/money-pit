"""Tests for money_pit.mcp.manifest — the live introspection path and the pinned-schema build-time gate.

live_manifest takes an injectable `list_tools` seam over the live Alpaca MCP server. Passing a stub
callable directly (never spawning a server) is the sanctioned boundary: it lets us pin the mapping and
the fail-closed ManifestUnavailableError offline. pinned_manifest takes an injectable schema_path seam;
feeding it an explicit unpinned-stub schema pins the build-time fail-closed guard independently of the
committed file's state. Assertions target the error TYPE, never message text.
"""

import json
from pathlib import Path

import pytest
from mcp.types import Tool

from money_pit.config import AlpacaCredentials
from money_pit.mcp.manifest import ManifestUnavailableError
from money_pit.mcp.manifest import live_manifest
from money_pit.mcp.manifest import pinned_manifest
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL


@pytest.fixture
def credentials() -> AlpacaCredentials:
    return AlpacaCredentials(api_key="the-key", secret_key="the-secret", paper=True)


@pytest.fixture
def unpinned_stub_schema_path(tmp_path: Path, stub_free_order_schema_path: Path) -> Path:
    schema: dict[str, object] = json.loads(stub_free_order_schema_path.read_text(encoding="utf-8"))
    schema[ALPACA_ORDER_SCHEMA_STUB_SENTINEL] = True
    path: Path = tmp_path / "alpaca_order_schema.json"
    _ = path.write_text(json.dumps(schema), encoding="utf-8")
    return path


def test_pinned_manifest_with_pinned_schema(stub_free_order_schema_path: Path) -> None:
    result = pinned_manifest(stub_free_order_schema_path)

    assert result
    assert all(isinstance(schema, dict) for schema in result.values())


def test_pinned_manifest_with_unpinned_stub_fails_closed(unpinned_stub_schema_path: Path) -> None:
    with pytest.raises(ManifestUnavailableError):
        _ = pinned_manifest(unpinned_stub_schema_path)


def test_pinned_manifest_with_missing_schema_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ManifestUnavailableError):
        _ = pinned_manifest(tmp_path / "not_yet_pinned.json")


@pytest.fixture
def place_stock_order_schema() -> dict[str, object]:
    return {"type": "object", "properties": {"symbol": {"type": "string"}}}


def test_live_manifest_with_available_tools(
    credentials: AlpacaCredentials, place_stock_order_schema: dict[str, object]
) -> None:
    def _list_tools(_creds: AlpacaCredentials) -> list[Tool]:
        return [Tool(name="place_stock_order", inputSchema=place_stock_order_schema)]

    result = live_manifest(credentials, list_tools=_list_tools)

    assert result == {"place_stock_order": place_stock_order_schema}


def test_live_manifest_with_introspection_failure(credentials: AlpacaCredentials) -> None:
    def _raise(_creds: AlpacaCredentials) -> list[Tool]:
        raise RuntimeError("connection refused")

    with pytest.raises(ManifestUnavailableError):
        _ = live_manifest(credentials, list_tools=_raise)
