"""Module containing the AlpacaWriteDeps dep type, its factory, and the connect-per-call OrderPlacer over the Alpaca MCP write server for the money_pit package."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp import ClientSession
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult
from mcp.types import ListToolsResult
from mcp.types import Tool

from money_pit.config import AlpacaCredentials
from money_pit.contracts import OrderPlacer
from money_pit.mcp.constants import PLACE_STOCK_ORDER_TOOL
from money_pit.pipeline.execution import OrderSubmissionError
from money_pit.schemas.action_steps import ExecutionParameters


_ALPACA_MCP_COMMAND: str = "alpaca-mcp-server"
_ALPACA_WRITE_TOOLSET: str = "trading"

_ORDER_ID_KEYS: tuple[str, ...] = ("id", "order_id", "broker_order_id", "client_order_id")


def _paper_flag(paper: bool) -> str:
    """Return the ALPACA_PAPER_TRADE env value for a paper/live boolean."""
    return "true" if paper else "false"


def _write_env(credentials: AlpacaCredentials) -> dict[str, str]:
    """Return the environment for spawning alpaca-mcp-server scoped to the write (trading) toolset."""
    return {
        "ALPACA_API_KEY": credentials.api_key,
        "ALPACA_SECRET_KEY": credentials.secret_key.get_secret_value(),
        "ALPACA_PAPER_TRADE": _paper_flag(credentials.paper),
        "ALPACA_TOOLSETS": _ALPACA_WRITE_TOOLSET,
    }


def _extract_order_id(structured: object) -> str:
    """Return the structured broker order id, raising OrderSubmissionError on a rejection or missing structured id."""
    if isinstance(structured, dict):
        if "error" in structured:
            raise OrderSubmissionError(f"Alpaca rejected the order: {structured['error']}.")
        for key in _ORDER_ID_KEYS:
            value: object = structured.get(key)
            if isinstance(value, str) and value:
                return value
    raise OrderSubmissionError("Alpaca returned no structured broker order id for the submission.")


@asynccontextmanager
async def _open_trading_session(credentials: AlpacaCredentials) -> AsyncIterator[ClientSession]:  # pragma: no cover
    """Spawn a fresh trading-scoped Alpaca MCP server and yield an initialized ClientSession."""
    params: StdioServerParameters = StdioServerParameters(
        command=_ALPACA_MCP_COMMAND, args=[], env=_write_env(credentials)
    )
    async with stdio_client(params) as (read_stream, write_stream), ClientSession(read_stream, write_stream) as session:
        _ = await session.initialize()
        yield session


async def _submit_order(credentials: AlpacaCredentials, arguments: dict[str, object]) -> str:  # pragma: no cover
    """Spawn a fresh Alpaca MCP write session, place the order, and return the broker order id."""
    async with _open_trading_session(credentials) as session:
        result: CallToolResult = await session.call_tool(PLACE_STOCK_ORDER_TOOL, arguments=arguments)
        if result.isError:
            raise OrderSubmissionError(f"Alpaca rejected the order: {_result_text(result.content)}.")
        return _extract_order_id(result.structuredContent)


async def _list_trading_tools(credentials: AlpacaCredentials) -> list[Tool]:  # pragma: no cover
    """Spawn a fresh Alpaca MCP write session and return its registered trading tools."""
    async with _open_trading_session(credentials) as session:
        result: ListToolsResult = await session.list_tools()
        return list(result.tools)


def list_write_tools(credentials: AlpacaCredentials) -> list[Tool]:
    """Return the trading-scoped tools registered on a freshly spawned Alpaca MCP write server."""
    return asyncio.run(_list_trading_tools(credentials))  # pragma: no cover


def _result_text(content: object) -> str:
    """Return the concatenated text of a tool result's content blocks, ignoring non-text blocks."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        text: object = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


@dataclass(frozen=True)
class AlpacaWriteDeps:
    """Connect-per-call OrderPlacer over the Alpaca MCP write server; credentials resolved by the caller."""

    credentials: AlpacaCredentials

    def __call__(self, params: ExecutionParameters) -> str:
        """Submit one equity order, returning the broker order id or raising OrderSubmissionError on rejection."""
        return asyncio.run(_submit_order(self.credentials, params.to_order_payload()))  # pragma: no cover


def make_alpaca_write_deps(credentials: AlpacaCredentials) -> OrderPlacer:
    """Return an OrderPlacer that submits equity orders through a fresh Alpaca MCP write session per call."""
    return AlpacaWriteDeps(credentials=credentials)
