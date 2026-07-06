"""Tests for money_pit.schemas.questions.Question signal_tier tightening (wave S8, theme T7).

Pins the loose-str -> SignalTier tightening: signal_tier must be a real SignalTier member
(coerced from its value or accepted as the enum), and an unknown tier string is rejected at
construction. Red today because the field is an unconstrained `str`.
"""

import pytest
from pydantic import ValidationError

from money_pit.schemas.enums import DataSourceToken, QuestionCategory, SignalTier
from money_pit.schemas.questions import Question


def _question(signal_tier: object) -> Question:
    return Question(
        id="Q001",
        category=QuestionCategory.MACRO_REGIME,
        question="What is the current ISM Manufacturing PMI reading?",
        signal_source="indicator:pmi",
        signal_tier=signal_tier,  # pyright: ignore[reportArgumentType]
        rationale="PMI above/below 50 signals expansion/contraction.",
        data_sources=[DataSourceToken.FRED_MCP],
        answer=None,
    )


@pytest.mark.parametrize("signal_tier", ["high", SignalTier.HIGH])
def test_question_with_valid_signal_tier_coerces_to_enum(signal_tier: object) -> None:
    question = _question(signal_tier)

    assert isinstance(question.signal_tier, SignalTier)


def test_question_with_invalid_signal_tier_raises() -> None:
    with pytest.raises(ValidationError):
        _question("not_a_tier")
