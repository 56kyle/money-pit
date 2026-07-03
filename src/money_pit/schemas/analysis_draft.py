"""AnalysisJudgment container — the §6.5 A4 judgment output consumed by pipeline/analysis.py."""
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.schemas.enums import ActionType
from money_pit.schemas.enums import ConvictionLevel
from money_pit.schemas.enums import Step1Disposition


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


class ThesisJudgment(BaseModel):
    """One surviving thesis from the A4 seven-step framework.

    The post-processor adds step_id, regime_tag, dollar_amount, and execution_parameters
    when materializing action_steps.json.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    instrument: str
    action_type: ActionType
    description: str
    group_id: str | None
    one_sentence_thesis: str
    expected_value: float
    conviction: ConvictionLevel
    scenario_table: ScenarioTable
    invalidation_conditions: list[InvalidationCondition]
    sizing_rationale: str
    disposition: Step1Disposition


class DroppedClaim(BaseModel):
    """A claim discarded during A4 analysis, recorded with the reason for the audit trail."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    reason: str


class MacroIndicatorReading(BaseModel):
    """A4's qualitative Step-2 reading of one macro indicator; consumed only by analysis.md."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    indicator: str
    reading: str
    favorable: bool | None


class AnalysisHalt(BaseModel):
    """Pure-data record that A4 could not complete a mandatory step; carries no TerminalState."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    reason: str


class AnalysisJudgment(BaseModel):
    """The §6.5 A4 output container: surviving theses, dropped claims, macro read, optional halt."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    theses: list[ThesisJudgment]
    dropped_claims: list[DroppedClaim]
    macro_read: list[MacroIndicatorReading]
    halt: AnalysisHalt | None
