"""Module exposing an alpaca-py-backed PortfolioFetcher for the money_pit package.

sector is resolved best-effort via yfinance and falls back to "unknown"; factor_tags and
correlated_overlaps are v0-deferred and always emitted empty.
"""

import requests
from alpaca.trading.client import TradingClient
from loguru import logger

from money_pit.config import AlpacaCredentials
from money_pit.contracts import PortfolioFetcher
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.portfolio import Position


_US_EQUITY_ASSET_CLASS: str = "us_equity"
_UNKNOWN_SECTOR: str = "unknown"


class NonEquityPositionError(Exception):
    """Raised when an Alpaca position is not a us_equity asset — the v0 snapshot is equities-only."""


def _asset_class_value(asset_class: object) -> str:
    """Return the underlying string of an alpaca-py asset_class, whether it is an enum or a bare string."""
    value: object = getattr(asset_class, "value", asset_class)
    return str(value)


def _resolve_sector(ticker: str) -> str:  # pragma: no cover
    """Return the yfinance sector for a ticker, best-effort, falling back to "unknown" (mirrors the _Direct* tools)."""
    import yfinance as yf  # pyright: ignore[reportMissingTypeStubs]

    try:
        info: dict[str, object] = yf.Ticker(ticker).info  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    except (requests.RequestException, OSError, KeyError, ValueError) as error:
        logger.debug("yfinance sector lookup failed for {ticker}; using {fallback}: {error}", ticker=ticker, fallback=_UNKNOWN_SECTOR, error=error)
        return _UNKNOWN_SECTOR
    sector: object = info.get("sector")  # pyright: ignore[reportUnknownMemberType]
    if isinstance(sector, str) and sector:
        return sector
    return _UNKNOWN_SECTOR


def _to_position(raw: object) -> Position:
    """Map one alpaca-py position onto our frozen Position, failing closed on a non-equity asset class."""
    if _asset_class_value(getattr(raw, "asset_class", None)) != _US_EQUITY_ASSET_CLASS:
        raise NonEquityPositionError(
            f"Position {getattr(raw, 'symbol', '?')!r} is not a us_equity asset; the v0 snapshot is equities-only."
        )
    ticker: str = str(getattr(raw, "symbol"))
    return Position(
        ticker=ticker,
        quantity=float(getattr(raw, "qty")),
        cost_basis=float(getattr(raw, "avg_entry_price")),
        current_value=float(getattr(raw, "market_value")),
        unrealized_pl=float(getattr(raw, "unrealized_pl")),
        sector=_resolve_sector(ticker),
        factor_tags=[],
    )


def _sector_weights(positions: list[Position], total_account_value: float) -> dict[str, float]:
    """Return each sector's fraction of total account value, computed deterministically over the positions."""
    if total_account_value <= 0:
        return {}
    sums: dict[str, float] = {}
    for position in positions:
        sums[position.sector] = sums.get(position.sector, 0.0) + position.current_value
    return {sector: value / total_account_value for sector, value in sums.items()}


def make_alpaca_portfolio_fetcher(credentials: AlpacaCredentials) -> PortfolioFetcher:
    """Return a PortfolioFetcher backed by the alpaca-py TradingClient, routed to paper or live per credentials."""
    client: TradingClient = TradingClient(
        api_key=credentials.api_key,
        secret_key=credentials.secret_key,
        paper=credentials.paper,
    )

    def fetch_portfolio(slug: str) -> PortfolioSnapshot:  # pragma: no cover
        account = client.get_account()  # pyright: ignore[reportUnknownMemberType]
        total_account_value: float = float(getattr(account, "portfolio_value"))
        positions: list[Position] = [_to_position(raw) for raw in client.get_all_positions()]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return PortfolioSnapshot(
            slug=slug,
            as_of=slug,
            total_account_value=total_account_value,
            available_cash=float(getattr(account, "cash")),
            positions=positions,
            sector_weights=_sector_weights(positions, total_account_value),
            correlated_overlaps=[],
        )

    return fetch_portfolio
