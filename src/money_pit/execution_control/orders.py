"""Module containing stable plan-aware order parameter construction."""

import re
from typing import Literal

from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import ProposedTrade


_MAX_CLIENT_ORDER_ID_LENGTH: int = 48
_SYMBOL_COMPONENT_PATTERN: re.Pattern[str] = re.compile(r"[^A-Za-z0-9]")
OrderSide = Literal["buy", "sell"]


def plan_client_order_id(plan: PortfolioPlan, trade_index: int, instrument: str) -> str:
    """Return a stable Alpaca-compatible identifier bound to a plan hash and trade index."""
    if trade_index < 0:
        raise ValueError("trade_index must be non-negative.")
    symbol: str = _SYMBOL_COMPONENT_PATTERN.sub("", instrument.upper())[:8] or "ASSET"
    identifier: str = f"mp-{plan.plan_hash[:24]}-{trade_index:04d}-{symbol}"
    return identifier[:_MAX_CLIENT_ORDER_ID_LENGTH]


def execution_parameters_for_trade(
    plan: PortfolioPlan,
    trade_index: int,
    trade: ProposedTrade,
) -> ExecutionParameters:
    """Convert a portfolio-plan trade into deterministic market-day order parameters."""
    side: OrderSide = _order_side(trade.side)
    return ExecutionParameters(
        symbol=trade.instrument,
        notional=None,
        qty=format(trade.quantity, ".15g"),
        side=side,
        type="market",
        time_in_force="day",
        client_order_id=plan_client_order_id(plan, trade_index, trade.instrument),
    )


def _order_side(value: str) -> OrderSide:
    """Return a validated broker order side."""
    if value == "buy" or value == "sell":
        return value
    raise ValueError(f"Unsupported order side: {value!r}.")
