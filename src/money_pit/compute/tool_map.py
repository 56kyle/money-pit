"""ActionType → Alpaca MCP tool name and compensating action mappings."""

from money_pit.schemas.enums import ActionType


ACTION_TYPE_TO_TOOL: dict[ActionType, str] = {
    ActionType.BUY: "place_order",
    ActionType.ADD: "place_order",
    ActionType.SELL: "place_order",
    ActionType.TRIM: "place_order",
}

COMPENSATING_ACTION: dict[ActionType, ActionType] = {
    ActionType.BUY: ActionType.SELL,
    ActionType.ADD: ActionType.TRIM,
    ActionType.SELL: ActionType.BUY,
    ActionType.TRIM: ActionType.ADD,
}
