"""Tests for the pure helpers in money_pit.mcp.clients — env building, arg remapping, and result parsing.

Covers the offline, deterministic seams only; the stdio-spawning session bodies are live-only and pinned by
the acceptance tier. Assertions target the OrderSubmissionError TYPE and concrete values, never message text.
"""

from dataclasses import dataclass

import pytest
from pydantic import SecretStr
from pytest import FixtureRequest

from money_pit.config import AlpacaCredentials
from money_pit.contracts import OrderPlacer
from money_pit.mcp.clients import _extract_order_id
from money_pit.mcp.clients import _result_text
from money_pit.mcp.clients import _write_env
from money_pit.mcp.clients import make_alpaca_write_deps
from money_pit.pipeline.execution import OrderSubmissionError


_SENTINEL_SECRET_KEY: str = "the-secret"


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
        AlpacaCredentials(api_key="the-key", secret_key=SecretStr(_SENTINEL_SECRET_KEY), paper=credentials__paper),
    )


def test__write_env_with_toolset_and_keys(credentials: AlpacaCredentials) -> None:
    env = _write_env(credentials)

    assert env["ALPACA_TOOLSETS"] == "trading"
    assert env["ALPACA_API_KEY"] == credentials.api_key
    assert env["ALPACA_SECRET_KEY"] == _SENTINEL_SECRET_KEY


def test_make_alpaca_write_deps_never_holds_the_secret_as_a_run_lifetime_plaintext_attribute(
    credentials: AlpacaCredentials,
) -> None:
    """Pin ADR 0032 decision #1 across the dep object: AlpacaWriteDeps outlives the run and has a default repr.

    _write_env unwraps inline, so the plaintext must not survive on the deps object or its nested credentials.
    """
    deps: OrderPlacer = make_alpaca_write_deps(credentials)

    assert _SENTINEL_SECRET_KEY not in repr(deps)
    assert _SENTINEL_SECRET_KEY not in str(vars(deps))


@pytest.mark.parametrize(
    ("credentials__paper", "expected"), [(True, "true"), (False, "false")], indirect=["credentials__paper"]
)
def test__write_env_with_paper_flag(credentials: AlpacaCredentials, expected: str) -> None:
    assert _write_env(credentials)["ALPACA_PAPER_TRADE"] == expected


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
