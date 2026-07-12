"""Module containing the ActionStep and ExecutionParameters post-processor output consumed by A5 in the money_pit package."""

from typing import ClassVar
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.analysis_draft import InvalidationCondition
from money_pit.schemas.analysis_draft import ScenarioTable
from money_pit.schemas.enums import ActionType
from money_pit.schemas.enums import ConvictionLevel
from money_pit.schemas.enums import RegimeTag


class ExecutionParameters(BaseModel):
    """Alpaca MCP order tool parameters; keys must match the official inputSchema literally."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    notional: str | None
    qty: str | None
    side: Literal["buy", "sell"]
    type: Literal["market"]
    time_in_force: Literal["day"]
    client_order_id: str

    def to_order_payload(self) -> dict[str, object]:
        """Return the exact order-tool payload dict, omitting None-valued optional fields."""
        payload: dict[str, object] = {
            "symbol": self.symbol,
            "side": self.side,
            "type": self.type,
            "time_in_force": self.time_in_force,
            "client_order_id": self.client_order_id,
        }
        if self.notional is not None:
            payload["notional"] = self.notional
        if self.qty is not None:
            payload["qty"] = self.qty
        return payload


class ActionStep(BaseModel):
    """A fully materialized, execution-ready action step; one element of action_steps.json."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    instrument: str
    action_type: ActionType
    description: str
    group_id: str | None
    execution_parameters: ExecutionParameters
    one_sentence_thesis: str
    regime_tag: RegimeTag
    expected_value: float
    scenario_table: ScenarioTable
    invalidation_conditions: list[InvalidationCondition]
    sizing_rationale: str
    conviction: ConvictionLevel
    step_failed: Literal[None] = None
