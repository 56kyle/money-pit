"""Module containing held-ETF single-name overlap detection for the money_pit package."""

from money_pit.schemas.portfolio import CorrelatedOverlap
from money_pit.schemas.portfolio import Position


ETF_HOLDS_SINGLE_NAME_NOTE = "ETF holds the single-name position"


def detect_etf_overlaps(
    positions: list[Position],
    etf_holdings: dict[str, list[str]],
) -> list[CorrelatedOverlap]:
    held_by_symbol: dict[str, str] = {position.ticker.upper(): position.ticker for position in positions}
    overlaps: list[CorrelatedOverlap] = []
    for etf, holdings in etf_holdings.items():
        for symbol in holdings:
            if symbol == etf:
                continue
            held_ticker = held_by_symbol.get(symbol.upper())
            if held_ticker is None or held_ticker == etf:
                continue
            overlaps.append(
                CorrelatedOverlap(tickers=[etf, held_ticker], note=ETF_HOLDS_SINGLE_NAME_NOTE),
            )
    return overlaps
