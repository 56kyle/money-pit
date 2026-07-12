"""Module containing the ActionType-plus-judgment to execution_parameters mapping with literal Alpaca MCP field names for the money_pit package."""

import math
from pathlib import Path
from typing import Literal

import jsonschema

from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_PATH
from money_pit.mcp.order_schema import load_order_schema
from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.enums import ActionType


_BUY_SIDES: frozenset[ActionType] = frozenset({ActionType.BUY, ActionType.ADD})

_SIDE_BUY: Literal["buy"] = "buy"
_SIDE_SELL: Literal["sell"] = "sell"
_ORDER_TYPE_MARKET: Literal["market"] = "market"
_TIME_IN_FORCE_DAY: Literal["day"] = "day"

_NOTIONAL_DECIMAL_PLACES: int = 2
_MIN_NOTIONAL_DOLLARS: float = 10 ** -_NOTIONAL_DECIMAL_PLACES


class InvalidExecutionAmountError(Exception):
    """Raised when the dollar amount for a notional order is not finite or is below the minimum representable notional."""


def build_execution_params(
    step_id: str,
    slug: str,
    symbol: str,
    action_type: ActionType,
    dollar_amount: float,
    *,
    schema_path: Path = ALPACA_ORDER_SCHEMA_PATH,
) -> ExecutionParameters:
    """Build an ExecutionParameters instance and validate its emitted payload against the pinned schema.

    Raises InvalidExecutionAmountError if dollar_amount is not finite or is below the
    minimum notional (one cent). Fails closed via load_order_schema (AlpacaOrderSchemaMissingError /
    AlpacaOrderSchemaMalformedError) if the schema is absent or malformed, and lets
    jsonschema.ValidationError propagate when the emitted payload violates the schema.
    """
    if not math.isfinite(dollar_amount) or dollar_amount < _MIN_NOTIONAL_DOLLARS:
        raise InvalidExecutionAmountError(
            f"Notional order requires a finite dollar amount of at least the minimum notional "
            f"({_MIN_NOTIONAL_DOLLARS:.{_NOTIONAL_DECIMAL_PLACES}f}), got {dollar_amount!r}."
        )
    schema: dict[str, object] = load_order_schema(schema_path)
    side: Literal["buy", "sell"] = _SIDE_BUY if action_type in _BUY_SIDES else _SIDE_SELL
    notional: str = f"{dollar_amount:.{_NOTIONAL_DECIMAL_PLACES}f}"
    params: ExecutionParameters = ExecutionParameters(
        symbol=symbol,
        notional=notional,
        qty=None,
        side=side,
        type=_ORDER_TYPE_MARKET,
        time_in_force=_TIME_IN_FORCE_DAY,
        client_order_id=f"{slug}:{step_id}",
    )
    jsonschema.validate(instance=params.to_order_payload(), schema=schema)
    return params
