"""Module containing the tool manifests consumed by A5 (the static `pinned_manifest` default and the opt-in `live_manifest`) for the money_pit package.

`pinned_manifest` is the *static pinned* contract-level record of which write tools exist for the
closed Alpaca write-tool set — it is not a live view of any running server, and remains the default
so A5 validates against a fixed set whose existence check is honest rather than assumed.

`live_manifest` now EXISTS as an opt-in: it introspects a freshly spawned Alpaca MCP write server
and reports the tools it actually registers. It fails closed with `ManifestUnavailableError` on any
connection or introspection failure, so an operator who opts in never trades against an assumed set.
"""

from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path

from mcp.types import Tool

from money_pit.compute.tool_map import ACTION_TYPE_TO_TOOL
from money_pit.config import AlpacaCredentials
from money_pit.contracts import ToolManifest
from money_pit.mcp.clients import list_write_tools
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_PATH
from money_pit.mcp.order_schema import AlpacaOrderSchemaError
from money_pit.mcp.order_schema import load_order_schema


class ManifestUnavailableError(Exception):
    """Raised when the tool manifest cannot be produced — A5 fails closed rather than assuming a tool exists."""


def pinned_manifest(
    schema_path: Path = ALPACA_ORDER_SCHEMA_PATH,
) -> Mapping[str, dict[str, object]]:
    """Return the static pinned manifest mapping each write tool to the pinned Alpaca order schema.

    Fails closed with `ManifestUnavailableError` if the pinned schema cannot be loaded.
    """
    try:
        schema: dict[str, object] = load_order_schema(schema_path)
    except AlpacaOrderSchemaError as err:
        raise ManifestUnavailableError(f"Pinned tool manifest unavailable: {err}") from err
    return dict.fromkeys(set(ACTION_TYPE_TO_TOOL.values()), schema)


def live_manifest(
    credentials: AlpacaCredentials,
    list_tools: Callable[[AlpacaCredentials], list[Tool]] = list_write_tools,
) -> ToolManifest:
    """Return a live manifest by introspecting a freshly spawned Alpaca MCP write server.

    Fails closed with `ManifestUnavailableError` on any connection or introspection failure.
    """
    try:
        tools: list[Tool] = list_tools(credentials)
    except Exception as err:
        raise ManifestUnavailableError(f"Live tool manifest unavailable: {err}") from err
    return {tool.name: tool.inputSchema for tool in tools}
