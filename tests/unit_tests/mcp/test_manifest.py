"""Tests for money_pit.mcp.manifest.live_manifest — the opt-in introspection path and its fail-closed gate.

live_manifest takes an injectable `list_tools` seam over the live Alpaca MCP server. Passing a stub
callable directly (never spawning a server) is the sanctioned boundary: it lets us pin the mapping and
the fail-closed ManifestUnavailableError offline. Assertions target the error TYPE, never message text.
"""

import pytest
from mcp.types import Tool

from money_pit.config import AlpacaCredentials
from money_pit.mcp.manifest import ManifestUnavailableError
from money_pit.mcp.manifest import live_manifest


@pytest.fixture
def credentials() -> AlpacaCredentials:
    return AlpacaCredentials(api_key="the-key", secret_key="the-secret", paper=True)


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
