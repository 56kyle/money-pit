"""Tests for the pure helpers in money_pit.mcp.clients — env building, arg remapping, and result parsing.

Covers the offline, deterministic seams only; the stdio-spawning session bodies are live-only and pinned by
the acceptance tier. Assertions target the OrderSubmissionError TYPE and concrete values, never message text.
"""

from dataclasses import dataclass

import pytest
from pytest import FixtureRequest

from money_pit.config import AlpacaCredentials
from money_pit.mcp.clients import _extract_order_id
from money_pit.mcp.clients import _order_arguments
from money_pit.mcp.clients import _result_text
from money_pit.mcp.clients import _write_env
from money_pit.pipeline.execution import OrderSubmissionError
from money_pit.schemas.action_steps import ExecutionParameters


@dataclass
class _TextBlock:
    """A minimal stand-in for an MCP text content block."""

    text: str


@dataclass
class _NonTextBlock:
    """A content block with no text attribute — _result_text must ignore it."""

    data: str


@pytest.fixture
def credentials__paper(request: FixtureRequest) -> bool:
    return getattr(request, "param", True)


@pytest.fixture
def credentials(request: FixtureRequest, credentials__paper: bool) -> AlpacaCredentials:
    return getattr(
        request,
        "param",
        AlpacaCredentials(api_key="the-key", secret_key="the-secret", paper=credentials__paper),
    )


def _execution_parameters(*, notional: float | None, quantity: float | None) -> ExecutionParameters:
    return ExecutionParameters(
        symbol="NVDA",
        notional=notional,
        quantity=quantity,
        side="buy",
        type="market",
        time_in_force="day",
        client_order_id="2026-01-01_00-00-00:A001",
    )


def test__write_env_with_toolset_and_keys(credentials: AlpacaCredentials) -> None:
    env = _write_env(credentials)

    assert env["ALPACA_TOOLSETS"] == "trading"
    assert env["ALPACA_API_KEY"] == credentials.api_key
    assert env["ALPACA_SECRET_KEY"] == credentials.secret_key


@pytest.mark.parametrize(
    ("credentials__paper", "expected"), [(True, "true"), (False, "false")], indirect=["credentials__paper"]
)
def test__write_env_with_paper_flag(credentials: AlpacaCredentials, expected: str) -> None:
    assert _write_env(credentials)["ALPACA_PAPER_TRADE"] == expected


def test__order_arguments_with_quantity_remapped_to_qty() -> None:
    params = _execution_parameters(notional=None, quantity=10.0)

    arguments = _order_arguments(params)

    assert arguments["qty"] == 10.0
    assert "quantity" not in arguments


def test__order_arguments_with_notional_only_keeps_notional() -> None:
    params = _execution_parameters(notional=1500.0, quantity=None)

    arguments = _order_arguments(params)

    assert arguments["notional"] == 1500.0
    assert "qty" not in arguments


def test__extract_order_id_with_rejection() -> None:
    with pytest.raises(OrderSubmissionError):
        _ = _extract_order_id({"error": "insufficient buying power"})


def test__extract_order_id_with_id_key() -> None:
    assert _extract_order_id({"id": "broker-123"}) == "broker-123"


def test__extract_order_id_with_no_structured_id_fails_closed() -> None:
    with pytest.raises(OrderSubmissionError):
        _ = _extract_order_id({"status": "accepted"})


def test__extract_order_id_with_empty_result() -> None:
    with pytest.raises(OrderSubmissionError):
        _ = _extract_order_id({})


def test__extract_order_id_with_non_dict_fails_closed() -> None:
    with pytest.raises(OrderSubmissionError):
        _ = _extract_order_id(None)


def test__result_text_concatenates_text_blocks_ignoring_non_text() -> None:
    content = [_TextBlock(text="first"), _NonTextBlock(data="binary"), _TextBlock(text="second")]

    assert _result_text(content) == "first\nsecond"


def test__result_text_with_non_list_returns_empty() -> None:
    assert _result_text("not a list") == ""
