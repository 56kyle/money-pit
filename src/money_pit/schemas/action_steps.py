"""ActionStep, ExecutionParameters, ActionSteps — post-processor output, consumed by A5."""
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from money_pit.schemas.analysis_draft import InvalidationCondition, ScenarioTable
from money_pit.schemas.enums import ActionType, ConvictionLevel, RegimeTag


class ExecutionParameters(BaseModel):
    """Alpaca MCP order tool parameters; keys must match the official inputSchema literally."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    notional: float | None
    quantity: float | None
    side: str
    type: str
    time_in_force: str
    client_order_id: str


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


class ActionSteps(BaseModel):
    """Container for all action steps in a run; steps=[] signals a clean no-trade day."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    steps: list[ActionStep]
