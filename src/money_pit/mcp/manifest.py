"""Static pinned tool manifest consumed by A5 (Phase 7 swaps this for live MCP-server introspection).

The manifest here is the *static pinned* contract-level record of which write tools exist
for the closed Alpaca write-tool set — it is not a live view of any running server. Phase 7
replaces the default provider with live introspection of registered MCP servers; until then
A5 validates against this fixed set so its existence check is honest rather than assumed.
"""
from collections.abc import Mapping
from pathlib import Path

from money_pit.compute.tool_map import ACTION_TYPE_TO_TOOL
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_PATH
from money_pit.mcp.order_schema import AlpacaOrderSchemaError
from money_pit.mcp.order_schema import load_order_schema


class ManifestUnavailableError(Exception):
    """Raised when the tool manifest cannot be produced — A5 fails closed rather than assuming a tool exists."""


def pinned_manifest(
    schema_path: Path = ALPACA_ORDER_SCHEMA_PATH,
) -> Mapping[str, dict[str, object]]:
    """Return the static pinned manifest mapping each write tool to the pinned Alpaca order schema, failing closed with `ManifestUnavailableError` if the pinned schema cannot be loaded."""
    try:
        schema: dict[str, object] = load_order_schema(schema_path)
    except AlpacaOrderSchemaError as err:
        raise ManifestUnavailableError(
            f"Pinned tool manifest unavailable: {err}"
        ) from err
    return {tool: schema for tool in set(ACTION_TYPE_TO_TOOL.values())}
