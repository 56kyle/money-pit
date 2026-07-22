"""Module exposing an alpaca-py-backed PortfolioFetcher for the money_pit package.

sector and factor_tags are derived best-effort from a position's yfinance `.info`, and
correlated_overlaps from held-ETF `funds_data`; every derivation is fail-soft and falls back
to "unknown"/empty rather than raising.
"""

from alpaca.trading.client import TradingClient

from money_pit.compute.factor_tags import FactorMetrics
from money_pit.compute.factor_tags import classify_factors
from money_pit.compute.overlaps import detect_etf_overlaps
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.contracts import PortfolioFetcher
from money_pit.market_data import as_float
from money_pit.market_data import fetch_etf_holdings
from money_pit.market_data import fetch_ticker_info
from money_pit.market_data import is_etf
from money_pit.market_data import resolve_sector
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.portfolio import Position


_US_EQUITY_ASSET_CLASS: str = "us_equity"


class NonEquityPositionError(Exception):
    """Raised when an Alpaca position is not a us_equity asset — the v0 snapshot is equities-only."""


def _asset_class_value(asset_class: object) -> str:
    """Return the underlying string of an alpaca-py asset_class, whether it is an enum or a bare string."""
    value: object = getattr(asset_class, "value", asset_class)
    return str(value)


def _factor_metrics_from_info(info: dict[str, object]) -> FactorMetrics:
    """Map a ticker's yfinance `.info` onto FactorMetrics, coercing each field best-effort to a finite float."""
    return FactorMetrics(
        trailing_pe=as_float(info.get("trailingPE")),
        price_to_book=as_float(info.get("priceToBook")),
        revenue_growth=as_float(info.get("revenueGrowth")),
        earnings_growth=as_float(info.get("earningsGrowth")),
        trailing_return=as_float(info.get("52WeekChange")),
        return_on_equity=as_float(info.get("returnOnEquity")),
        profit_margin=as_float(info.get("profitMargins")),
        beta=as_float(info.get("beta")),
    )


def _to_position(raw: object, info: dict[str, object], config: Config) -> Position:
    """Map one alpaca-py position onto our frozen Position, failing closed on a non-equity asset class."""
    if _asset_class_value(getattr(raw, "asset_class", None)) != _US_EQUITY_ASSET_CLASS:
        raise NonEquityPositionError(
            f"Position {getattr(raw, 'symbol', '?')!r} is not a us_equity asset; the v0 snapshot is equities-only."
        )
    return Position(
        ticker=str(raw.symbol),
        quantity=float(raw.qty),
        cost_basis=float(raw.avg_entry_price),
        current_value=float(raw.market_value),
        unrealized_pl=float(raw.unrealized_pl),
        sector=resolve_sector(info),
        factor_tags=classify_factors(_factor_metrics_from_info(info), config),
    )


def _sector_weights(positions: list[Position], total_account_value: float) -> dict[str, float]:
    """Return each sector's fraction of total account value, computed deterministically over the positions."""
    if total_account_value <= 0:
        return {}
    sums: dict[str, float] = {}
    for position in positions:
        sums[position.sector] = sums.get(position.sector, 0.0) + position.current_value
    return {sector: value / total_account_value for sector, value in sums.items()}


def make_alpaca_portfolio_fetcher(credentials: AlpacaCredentials, config: Config) -> PortfolioFetcher:
    """Return a PortfolioFetcher backed by the alpaca-py TradingClient, routed to paper or live per credentials."""
    client: TradingClient = TradingClient(
        api_key=credentials.api_key,
        secret_key=credentials.secret_key.get_secret_value(),
        paper=credentials.paper,
    )

    def fetch_portfolio(slug: str) -> PortfolioSnapshot:  # pragma: no cover
        account = client.get_account()  # pyright: ignore[reportUnknownMemberType]
        total_account_value: float = float(account.portfolio_value)
        positions: list[Position] = []
        etf_tickers: list[str] = []
        for raw in client.get_all_positions():  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            info: dict[str, object] = fetch_ticker_info(raw.symbol)  # pyright: ignore[reportUnknownArgumentType]
            positions.append(_to_position(raw, info, config))
            if is_etf(info):
                etf_tickers.append(str(raw.symbol))
        etf_holdings: dict[str, list[str]] = fetch_etf_holdings(etf_tickers)
        return PortfolioSnapshot(
            slug=slug,
            as_of=slug,
            total_account_value=total_account_value,
            available_cash=float(account.cash),
            positions=positions,
            sector_weights=_sector_weights(positions, total_account_value),
            correlated_overlaps=detect_etf_overlaps(positions, etf_holdings),
            etf_holdings=etf_holdings,
        )

    return fetch_portfolio
