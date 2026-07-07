"""Command-line interface."""

import json

import typer
from mcp.types import Tool

from money_pit.config import AlpacaCredentials
from money_pit.config import CredentialResolutionError
from money_pit.config import load_config
from money_pit.config import resolve_alpaca_credentials
from money_pit.mcp.clients import list_write_tools
from money_pit.mcp.constants import PLACE_STOCK_ORDER_TOOL
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_PATH
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL


app: typer.Typer = typer.Typer()


@app.command(name="money-pit")
def main() -> None:
    """Money Pit."""


@app.command(name="pin-order-schema")
def pin_order_schema() -> None:
    """Introspect the live place_stock_order tool and pin its inputSchema to the committed schema path."""
    try:
        credentials: AlpacaCredentials = resolve_alpaca_credentials(load_config())
    except CredentialResolutionError as error:
        typer.echo(f"Cannot resolve Alpaca credentials: {error}", err=True)
        raise typer.Exit(code=1) from error

    try:
        tools: list[Tool] = list_write_tools(credentials)
    except Exception as error:
        typer.echo(f"Failed to introspect the Alpaca MCP write server: {error}", err=True)
        raise typer.Exit(code=1) from error

    schema: dict[str, object] | None = next(
        (tool.inputSchema for tool in tools if tool.name == PLACE_STOCK_ORDER_TOOL), None
    )
    if schema is None:
        typer.echo(
            f"Tool {PLACE_STOCK_ORDER_TOOL!r} is absent from the Alpaca MCP write server;"
            + " refusing to write a partial schema.",
            err=True,
        )
        raise typer.Exit(code=1)

    pinned: dict[str, object] = dict(schema)
    _ = pinned.pop(ALPACA_ORDER_SCHEMA_STUB_SENTINEL, None)
    _ = ALPACA_ORDER_SCHEMA_PATH.write_text(json.dumps(pinned, indent=2), encoding="utf-8")
    typer.echo(f"Pinned {PLACE_STOCK_ORDER_TOOL} inputSchema to {ALPACA_ORDER_SCHEMA_PATH}")


if __name__ == "__main__":
    app()  # pragma: no cover
