"""Opt-in live acceptance smoke tests against real Alpaca paper and Gmail SMTP.

Every test here is marked `live` (deselected by default via addopts `-m 'not live'`) and additionally skipped
unless MONEY_PIT_LIVE=1, so a bare `-m live` run without an operator opt-in skips cleanly rather than erroring
or moving capital. Run explicitly with, e.g., `nox -s tests-python -- -m live` and MONEY_PIT_LIVE=1 set.
"""

import os

import pytest

from money_pit.alpaca_portfolio import make_alpaca_portfolio_fetcher
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.contracts import EmailSender
from money_pit.contracts import PortfolioFetcher
from money_pit.email_sender import make_gmail_email_sender
from money_pit.mcp.clients import list_write_tools
from money_pit.mcp.manifest import live_manifest
from money_pit.schemas.portfolio import PortfolioSnapshot


_PLACE_STOCK_ORDER_TOOL: str = "place_stock_order"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("MONEY_PIT_LIVE") != "1",
        reason="live tier is opt-in; set MONEY_PIT_LIVE=1 (with real Alpaca paper / Gmail creds) to run",
    ),
]


def test_live_portfolio_snapshot_validates(live_credentials: AlpacaCredentials) -> None:
    fetch_portfolio: PortfolioFetcher = make_alpaca_portfolio_fetcher(live_credentials)

    snapshot: PortfolioSnapshot = fetch_portfolio("2026-01-01_00-00-00")

    assert isinstance(snapshot, PortfolioSnapshot)
    _ = PortfolioSnapshot.model_validate(snapshot.model_dump())


def test_live_write_tools_include_place_stock_order(live_credentials: AlpacaCredentials) -> None:
    tools = list_write_tools(live_credentials)

    assert _PLACE_STOCK_ORDER_TOOL in {tool.name for tool in tools}


def test_live_manifest_includes_place_stock_order(live_credentials: AlpacaCredentials) -> None:
    manifest = live_manifest(live_credentials)

    assert _PLACE_STOCK_ORDER_TOOL in manifest


def test_live_gmail_email_smoke(live_config: Config) -> None:
    send_email: EmailSender = make_gmail_email_sender(live_config)

    send_email("money-pit live acceptance smoke", "This is a live acceptance-tier smoke email.")
