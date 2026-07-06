"""Tests for money_pit.mcp.manifest.live_manifest — the opt-in introspection path and its fail-closed gate.

live_manifest delegates to list_write_tools, our own seam over the live Alpaca MCP server. Monkeypatching
that seam (never spawning a server) is the sanctioned boundary: it lets us pin the mapping and the
fail-closed ManifestUnavailableError offline. Assertions target the error TYPE, never message text.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from pytest import MonkeyPatch

from money_pit.config import AlpacaCredentials
from money_pit.mcp import manifest as mcp_manifest
from money_pit.mcp.manifest import ManifestUnavailableError
from money_pit.mcp.manifest import live_manifest


@dataclass
class _FakeTool:
    """A minimal stand-in for mcp.types.Tool carrying just the fields live_manifest reads."""

    name: str
    inputSchema: dict[str, object]  # noqa: N815 - mirrors the mcp.types.Tool attribute name


@pytest.fixture
def credentials() -> AlpacaCredentials:
    return AlpacaCredentials(api_key="the-key", secret_key="the-secret", paper=True)


@pytest.fixture
def place_stock_order_schema() -> dict[str, object]:
    return {"type": "object", "properties": {"symbol": {"type": "string"}}}


def _patch_list_write_tools(monkeypatch: MonkeyPatch, replacement: Callable[[AlpacaCredentials], object]) -> None:
    monkeypatch.setattr(mcp_manifest, "list_write_tools", replacement)


def test_live_manifest_with_available_tools(
    monkeypatch: MonkeyPatch, credentials: AlpacaCredentials, place_stock_order_schema: dict[str, object]
) -> None:
    _patch_list_write_tools(monkeypatch, lambda _creds: [_FakeTool("place_stock_order", place_stock_order_schema)])

    result = live_manifest(credentials)

    assert result == {"place_stock_order": place_stock_order_schema}


def test_live_manifest_with_introspection_failure(monkeypatch: MonkeyPatch, credentials: AlpacaCredentials) -> None:
    def _raise(_creds: AlpacaCredentials) -> list[object]:
        raise RuntimeError("connection refused")

    _patch_list_write_tools(monkeypatch, _raise)

    with pytest.raises(ManifestUnavailableError):
        _ = live_manifest(credentials)
