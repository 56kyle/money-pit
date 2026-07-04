"""DraftQuestion — A2 LLM output: claim-specific thesis_validation and invalidation_conditions questions only."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import QuestionCategory


class DraftQuestion(BaseModel):
    """One claim-specific question authored by the A2 LLM core.

    Templated question types (macro_regime, current_events, portfolio_gap) are
    assembled by the pipeline node without a model call.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    category: QuestionCategory
    question: str
    signal_source: str
    rationale: str
