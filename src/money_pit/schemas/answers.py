"""Answer, InitialAnswers — initial_answers.json contract."""
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from money_pit.schemas.enums import Confidence, QuestionCategory
from money_pit.schemas.provenance import SourceRef


class Answer(BaseModel):
    """A single retrieved-and-synthesized answer, carried through to Agent 4."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    question_id: str
    question: str
    category: QuestionCategory
    signal_source: str
    signal_tier: str
    answer: str
    confidence: Confidence
    sources_used: list[str]
    data_retrieved: dict[str, object] | None
    limitations: str


class InitialAnswers(BaseModel):
    """Agent 3 output; written to initial_answers.json."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    sources: list[SourceRef]
    answers: list[Answer]
