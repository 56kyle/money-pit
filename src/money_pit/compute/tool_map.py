"""Module containing the ActionType-to-Alpaca-MCP tool-name and compensating-action mappings for the money_pit package."""

from money_pit.mcp.constants import PLACE_STOCK_ORDER_TOOL
from money_pit.schemas.enums import ActionType


# Equities-only scope: see ADR 0008.
ACTION_TYPE_TO_TOOL: dict[ActionType, str] = {
    ActionType.BUY: PLACE_STOCK_ORDER_TOOL,
    ActionType.ADD: PLACE_STOCK_ORDER_TOOL,
    ActionType.SELL: PLACE_STOCK_ORDER_TOOL,
    ActionType.TRIM: PLACE_STOCK_ORDER_TOOL,
}

COMPENSATING_ACTION: dict[ActionType, ActionType] = {
    ActionType.BUY: ActionType.SELL,
    ActionType.ADD: ActionType.TRIM,
    ActionType.SELL: ActionType.BUY,
    ActionType.TRIM: ActionType.ADD,
}
