"""Tests for money_pit.schemas.answer_draft."""
import pytest
from pydantic import ValidationError

from money_pit.schemas.answer_draft import AnswerDraft
from money_pit.schemas.enums import Confidence, DataSourceToken


def test_answer_draft_rejects_confidence_field() -> None:
    with pytest.raises(ValidationError):
        AnswerDraft(
            question_id="Q001",
            answer="A synthesized answer.",
            sources_used=[DataSourceToken.FRED_MCP],
            data_retrieved={"value": 1.23},
            limitations="none",
            confidence=Confidence.HIGH,
        )
