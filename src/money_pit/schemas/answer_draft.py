"""Module containing AnswerDraft, the A3 LLM output of answers to open-ended questions requiring relevance judgment, for the money_pit package."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import DataSourceToken


class AnswerDraft(BaseModel):
    """Open-ended answer synthesis from the A3 LLM core.

    Deterministic retrieval (named FRED series, current price, P/E) is performed
    by the pipeline node; only results requiring relevance judgment flow through here.
    Confidence is code-derived from sources_used by the node, never authored by the LLM.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    question_id: str
    answer: str
    sources_used: list[DataSourceToken]
    data_retrieved: dict[str, object] | None
    limitations: str
