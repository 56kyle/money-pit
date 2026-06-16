"""Module containing logic for working with options."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import TypedDict

from pandas import DatetimeIndex
from pandas import Series
from pandas._libs import NaTType
from pandas.core.tools.datetimes import DatetimeScalar
from pydantic import Field
from typing_extensions import Annotated

import numpy as np
import pandas as pd
import yfinance as yf
from pydantic import BaseModel


class OptionContract(BaseModel):
    """Represents an Option Contract (Call, Put) in Yahoo Finance."""
    contract_symbol: Annotated[str, Field(serialization_alias="contractSymbol")]
    strike: float
    currency: str
    last_price: Annotated[float, Field(serialization_alias="lastPrice")]
    change: float
    percent_change: Annotated[float, Field(serialization_alias="percentChange")]
    volume: int
    open_interest: Annotated[int, Field(serialization_alias="openInterest")]
    bid: float
    ask: float
    contract_size: Annotated[str, Field(serialization_alias="contractSize")]
    expiration: int
    last_trade_date: Annotated[int, Field(serialization_alias="lastTradeDate")]
    implied_volatility: Annotated[float, Field(serialization_alias="impliedVolatility")]
    in_the_money: Annotated[bool, Field(serialization_alias="inTheMoney")]


class OptionChain(TypedDict):
    """Represents a Yahoo Finance option chain."""
    calls: pd.DataFrame
    puts: pd.DataFrame
    underlying: dict[str, Any]


def get_option_chain(symbol: str, target_expiration: datetime) -> OptionChain:
    """Get option chain data for a symbol"""
    ticker: yf.Ticker = yf.Ticker(symbol)

    # Get all available expiration dates
    expirations: tuple[str, ...] = ticker.options

    # Find the closest expiration to our target
    exp_dates: DatetimeIndex | Series | DatetimeScalar | NaTType | None = pd.to_datetime(expirations)
    if exp_dates is None:
        raise ValueError(f"No expirations found.")
    closest_exp: str = expirations[np.argmin(abs(exp_dates - target_expiration))]

    # Get option chain for that expiration
    opt_chain: OptionChain = ticker.option_chain(closest_exp)
    return opt_chain


if __name__ == "__main__":
    target_expiration: datetime = datetime(2026, 3, 20)
    info: OptionChain = get_option_chain("SPY", target_expiration)
    print(info)
