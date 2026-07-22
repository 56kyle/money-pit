"""Tests for money_pit.mcp.manifest — the live introspection path and the pinned-schema build-time gate.

pinned_manifest loads the pinned default schema with no seam and propagates the loader's typed
AlpacaOrderSchemaError (missing / malformed / not-pinned) directly, with no wrap; that
absent/malformed/unpinned-stub precedence is pinned at the loader in test_order_schema.py.
live_manifest takes an injectable `list_tools` seam over the live Alpaca MCP server. Passing a stub
callable directly (never spawning a server) is the sanctioned boundary: it lets us pin the mapping and
its fail-closed ManifestUnavailableError contract offline. Assertions target the error TYPE, never
message text.
"""

import pytest
from mcp.types import Tool
from pydantic import SecretStr

from money_pit.config import AlpacaCredentials
from money_pit.mcp.manifest import ManifestUnavailableError
from money_pit.mcp.manifest import live_manifest
from money_pit.mcp.manifest import pinned_manifest


@pytest.fixture
def credentials() -> AlpacaCredentials:
    return AlpacaCredentials(api_key="the-key", secret_key=SecretStr("the-secret"), paper=True)


def test_pinned_manifest_with_pinned_schema() -> None:
    result = pinned_manifest()

    assert result
    assert all(isinstance(schema, dict) for schema in result.values())


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
