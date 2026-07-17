"""Opt-in live integration smoke tests against real Alpaca paper and Gmail SMTP.

Every test here is marked `live` (deselected by default via addopts `-m 'not live'`) and additionally skipped
unless MONEY_PIT_LIVE=1, so a bare `-m live` run without an operator opt-in skips cleanly rather than erroring
or moving capital. Run explicitly with, e.g., `nox -s tests-python -- -m live` and MONEY_PIT_LIVE=1 set.
"""

import os
import time
import uuid
from typing import TYPE_CHECKING

import pytest

from money_pit.alpaca_orders import make_alpaca_fill_observer
from money_pit.alpaca_portfolio import make_alpaca_portfolio_fetcher
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.email_sender import make_gmail_email_sender
from money_pit.mcp.clients import list_write_tools
from money_pit.mcp.clients import make_alpaca_write_deps
from money_pit.mcp.constants import PLACE_STOCK_ORDER_TOOL
from money_pit.mcp.manifest import live_manifest
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL
from money_pit.mcp.order_schema import load_order_schema
from money_pit.pipeline.execution import _poll_fill
from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.fills import FillObservation
from money_pit.schemas.portfolio import PortfolioSnapshot


if TYPE_CHECKING:
    from money_pit.contracts import EmailSender
    from money_pit.contracts import PortfolioFetcher


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("MONEY_PIT_LIVE") != "1",
        reason="live tier is opt-in; set MONEY_PIT_LIVE=1 (with real Alpaca paper / Gmail creds) to run",
    ),
]


def test_live_portfolio_snapshot_validates(
    live_credentials: AlpacaCredentials, live_config: Config
) -> None:
    fetch_portfolio: PortfolioFetcher = make_alpaca_portfolio_fetcher(live_credentials, live_config)

    snapshot: PortfolioSnapshot = fetch_portfolio("2026-01-01_00-00-00")

    assert isinstance(snapshot, PortfolioSnapshot)
    _ = PortfolioSnapshot.model_validate(snapshot.model_dump())


def test_live_write_tools_include_place_stock_order(live_credentials: AlpacaCredentials) -> None:
    tools = list_write_tools(live_credentials)

    assert PLACE_STOCK_ORDER_TOOL in {tool.name for tool in tools}


def test_live_manifest_includes_place_stock_order(live_credentials: AlpacaCredentials) -> None:
    manifest = live_manifest(live_credentials)

    assert PLACE_STOCK_ORDER_TOOL in manifest


def test_live_place_stock_order_schema_matches_pinned(live_credentials: AlpacaCredentials) -> None:
    """Trip the wire when the live Alpaca place_stock_order inputSchema drifts from the committed pin.

    Read-only introspection (no order placed, no capital moved). Normalizes the live schema exactly as the
    pin-order-schema CLI does (dropping the stub sentinel) so the comparison is apples-to-apples, then asserts a
    plain dict `==`: any field-level change (added/removed/renamed properties, type changes, description edits)
    fails here so the operator re-pins/reconciles before a real live order rejects against a stale schema.
    """
    live_schema = live_manifest(live_credentials)[PLACE_STOCK_ORDER_TOOL]
    expected = dict(live_schema)
    _ = expected.pop(ALPACA_ORDER_SCHEMA_STUB_SENTINEL, None)

    assert load_order_schema() == expected


def test_live_gmail_email_smoke(live_config: Config) -> None:
    send_email: EmailSender = make_gmail_email_sender(live_config)

    send_email("money-pit live integration smoke", "This is a live integration-tier smoke email.")


def test_live_paper_order_fill_observed(live_credentials: AlpacaCredentials) -> None:
    """Place a tiny $1 SPY market order on the paper account and observe its fill end-to-end.

    Opt-in live tier only. Uses a unique client_order_id so a rerun is a fresh order rather than a 422
    duplicate, and does not clean the order up (paper account; $1 notional; unique id). Observation goes
    through the real _poll_fill loop with a short timeout so the just-submitted 404 race resolves and the
    test is honest whether the market is open (fills) or closed (stays SUBMITTED).
    """
    assert live_credentials.paper, "refusing to place a live order: credentials are not paper-routed"
    place_order = make_alpaca_write_deps(live_credentials)
    observe_fill = make_alpaca_fill_observer(live_credentials)

    client_order_id: str = f"money-pit-livetest-{uuid.uuid4().hex}"
    params = ExecutionParameters(
        symbol="SPY",
        notional="1.00",
        qty=None,
        side="buy",
        type="market",
        time_in_force="day",
        client_order_id=client_order_id,
    )

    broker_order_id = place_order(params)

    assert isinstance(broker_order_id, str)
    assert broker_order_id

    obs = _poll_fill(
        observe_fill,
        client_order_id,
        interval=1.0,
        timeout=15.0,
        sleep=time.sleep,
        monotonic=time.monotonic,
    )

    assert isinstance(obs, FillObservation)
    assert isinstance(obs.status, str)
    assert obs.status
    assert obs.phase in {
        ExecutionPhase.FILLED,
        ExecutionPhase.PARTIALLY_FILLED,
        ExecutionPhase.SUBMITTED,
    }
    if obs.phase is ExecutionPhase.FILLED:
        assert obs.filled_qty is not None
        assert obs.filled_avg_price is not None
        assert obs.realized_notional is not None
