"""Question, SignalSummary, InitialQuestions — initial_questions.json contract."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import DataSourceToken
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import SignalTier


INDICATOR_PREFIX: str = "indicator:"


class Question(BaseModel):
    """A single research question to be answered by Agent 3."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    id: str
    category: QuestionCategory
    question: str
    signal_source: str | None
    signal_tier: SignalTier
    rationale: str
    data_sources: list[DataSourceToken]
    answer: None


class SignalSummary(BaseModel):
    """Tier-bucketed counts of claims that drove question generation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    high_signal_count: int
    medium_signal_count: int
    low_signal_count: int
    questions_generated: int


class InitialQuestions(BaseModel):
    """Agent 2 output; written to initial_questions.json."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    generated_at: str
    signal_summary: SignalSummary
    questions: list[Question]
    error: str | None
