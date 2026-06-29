"""AnalysisJudgment — LLM draft from A4, input to the post-processor in pipeline/analysis.py."""
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from money_pit.schemas.enums import ActionType, ConvictionLevel


class Scenario(BaseModel):
    """One scenario (bull/base/bear) within a three-point return distribution.

    The JSON field name for return_pct is "return" (matching agent_4.md output contract).
    populate_by_name=True allows Python construction via the return_pct keyword argument.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    probability: int
    return_pct: float = Field(alias="return")
    timeframe: str | None
    confirming_metric: str | None
    mechanism: str | None
    max_drawdown: float | None


class ScenarioTable(BaseModel):
    """Bull/base/bear scenario distribution for one thesis."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    bull: Scenario
    base: Scenario
    bear: Scenario


class InvalidationCondition(BaseModel):
    """A specific, observable condition that, if met, triggers a portfolio action."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    condition: str
    action: str


class AnalysisJudgment(BaseModel):
    """One judgment object from the A4 LLM core.

    The post-processor adds step_id, regime_tag, dollar_amount, and execution_parameters
    when materializing action_steps.json. Fields are nullable to accommodate the halt
    object (step_failed non-null, all others null).
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    claim_id: str | None
    instrument: str | None
    action_type: ActionType | None
    description: str | None
    group_id: str | None
    one_sentence_thesis: str | None
    expected_value: float | None
    conviction: ConvictionLevel | None
    scenario_table: ScenarioTable | None
    invalidation_conditions: list[InvalidationCondition]
    sizing_rationale: str | None
    step_failed: str | None
