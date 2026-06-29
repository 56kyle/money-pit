"""ActionType + judgment → execution_parameters with literal Alpaca MCP field names."""
import json
from pathlib import Path
from typing import cast

from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.enums import ActionType

_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent.parent.parent / "mcp" / "alpaca_order_schema.json"
)

_BUY_SIDES: frozenset[ActionType] = frozenset({ActionType.BUY, ActionType.ADD})


def _load_alpaca_schema() -> dict[str, object]:
    """Load and return the pinned Alpaca order JSON schema.

    Raises FileNotFoundError if the schema file has not yet been pinned from
    the live Alpaca MCP server.
    """
    if not _SCHEMA_PATH.exists():
        raise FileNotFoundError(
            f"Alpaca order schema not yet pinned. Expected at: {_SCHEMA_PATH}."
            + " Run the integration step to fetch it from the live Alpaca MCP server."
        )
    raw: object = cast(object, json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise TypeError(
            f"Expected JSON object in {_SCHEMA_PATH}, got {type(raw).__name__}"
        )
    return cast(dict[str, object], raw)


def build_execution_params(
    step_id: str,
    slug: str,
    symbol: str,
    action_type: ActionType,
    dollar_amount: float,
) -> ExecutionParameters:
    """Build an ExecutionParameters instance for the given action step.

    Raises FileNotFoundError if mcp/alpaca_order_schema.json has not been
    pinned; this is intentional — it keeps CI red until the schema is locked.
    """
    _ = _load_alpaca_schema()
    side: str = "buy" if action_type in _BUY_SIDES else "sell"
    return ExecutionParameters(
        symbol=symbol,
        notional=dollar_amount,
        quantity=None,
        side=side,
        type="market",
        time_in_force="day",
        client_order_id=f"{slug}:{step_id}",
    )
