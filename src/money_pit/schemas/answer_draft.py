"""AnswerDraft — A3 LLM output: answers to open-ended questions requiring relevance judgment."""
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from money_pit.schemas.enums import Confidence


class AnswerDraft(BaseModel):
    """Open-ended answer synthesis from the A3 LLM core.

    Deterministic retrieval (named FRED series, current price, P/E) is performed
    by the pipeline node; only results requiring relevance judgment flow through here.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    question_id: str
    answer: str
    confidence: Confidence
    sources_used: list[str]
    data_retrieved: dict[str, object] | None
    limitations: str
