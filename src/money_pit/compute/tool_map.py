"""ActionType → Alpaca MCP tool name and compensating action mappings."""

from money_pit.schemas.enums import ActionType


# Equities-only scope: see ADR 0008.
ACTION_TYPE_TO_TOOL: dict[ActionType, str] = {
    ActionType.BUY: "place_stock_order",
    ActionType.ADD: "place_stock_order",
    ActionType.SELL: "place_stock_order",
    ActionType.TRIM: "place_stock_order",
}

COMPENSATING_ACTION: dict[ActionType, ActionType] = {
    ActionType.BUY: ActionType.SELL,
    ActionType.ADD: ActionType.TRIM,
    ActionType.SELL: ActionType.BUY,
    ActionType.TRIM: ActionType.ADD,
}
