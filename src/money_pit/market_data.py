"""Module containing neutral yfinance-backed reference-data helpers used across the money_pit package.

Sector, ETF classification, and factor-input coercion are derived best-effort from a ticker's
yfinance `.info`, and top holdings from an ETF's funds_data; every derivation is fail-soft and falls
back to "unknown"/empty rather than raising. This module is a neutral adapter: it depends on no
alpaca-py, and only on the cycle-free money_pit.schemas/contracts leaf for its InstrumentFacts result.
"""

import math

import requests
from loguru import logger

from money_pit.contracts import ResolveInstrumentFacts
from money_pit.schemas.instrument import InstrumentFacts


UNKNOWN_SECTOR: str = "unknown"
ETF_QUOTE_TYPE: str = "ETF"


def fetch_ticker_info(ticker: str) -> dict[str, object]:  # pragma: no cover
    """Return a ticker's yfinance `.info`, best-effort, falling back to an empty mapping on any lookup failure."""
    import yfinance as yf  # pyright: ignore[reportMissingTypeStubs]

    try:
        info: dict[str, object] = yf.Ticker(ticker).info  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    except (requests.RequestException, OSError, KeyError, ValueError) as error:
        logger.debug("yfinance info lookup failed for {ticker}: {error}", ticker=ticker, error=error)
        return {}
    return info


def fetch_etf_holdings(tickers: list[str]) -> dict[str, list[str]]:  # pragma: no cover
    """Return each ETF ticker's top-holding symbols from yfinance funds_data, omitting names that yield none."""
    import yfinance as yf  # pyright: ignore[reportMissingTypeStubs]

    holdings: dict[str, list[str]] = {}
    for ticker in tickers:
        try:
            top_holdings = yf.Ticker(ticker).funds_data.top_holdings  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            symbols: list[str] = [str(symbol) for symbol in top_holdings.index]  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
        except Exception as error:  # noqa: BLE001
            logger.debug("yfinance holdings lookup failed for {ticker}: {error}", ticker=ticker, error=error)
            continue
        if symbols:
            holdings[ticker] = symbols
    return holdings


def as_float(value: object) -> float | None:
    """Return value as a finite float, or None when it is absent or not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number: float = float(value)
    if not math.isfinite(number):
        return None
    return number


def resolve_sector(info: dict[str, object]) -> str:
    """Return the yfinance sector from a ticker's info, falling back to "unknown" when absent or blank."""
    sector: object = info.get("sector")
    if isinstance(sector, str) and sector:
        return sector
    return UNKNOWN_SECTOR


def is_etf(info: dict[str, object]) -> bool:
    """Return whether a ticker's yfinance quoteType marks it as an ETF."""
    quote_type: object = info.get("quoteType")
    return isinstance(quote_type, str) and quote_type.upper() == ETF_QUOTE_TYPE


def make_yfinance_instrument_resolver() -> ResolveInstrumentFacts:
    """Return a ResolveInstrumentFacts backed purely by yfinance, resolving a candidate ticker's sector and ETF facts."""

    def resolve(ticker: str) -> InstrumentFacts:  # pragma: no cover
        info: dict[str, object] = fetch_ticker_info(ticker)
        etf: bool = is_etf(info)
        holdings: list[str] = fetch_etf_holdings([ticker]).get(ticker, []) if etf else []
        return InstrumentFacts(sector=resolve_sector(info), is_etf=etf, holdings=holdings)

    return resolve
