"""Tests for money_pit.schemas.answers.Answer signal_tier tightening (wave S8, theme T7).

Pins the loose-str -> SignalTier tightening: signal_tier must be a real SignalTier member
(coerced from its value or accepted as the enum), and an unknown tier string is rejected at
construction. Red today because the field is an unconstrained `str`.
"""
import pytest
from pydantic import ValidationError

from money_pit.schemas.answers import Answer
from money_pit.schemas.enums import Confidence, DataSourceToken, QuestionCategory, SignalTier


def _answer(signal_tier: object) -> Answer:
    return Answer(
        question_id="Q001",
        question="What is the current ISM Manufacturing PMI reading?",
        category=QuestionCategory.MACRO_REGIME,
        signal_source="indicator:pmi",
        signal_tier=signal_tier,  # pyright: ignore[reportArgumentType]
        answer="The latest ISM Manufacturing PMI reading is 48.7.",
        confidence=Confidence.HIGH,
        sources_used=[DataSourceToken.FRED_MCP],
        data_retrieved={"pmi": 48.7},
        limitations="none",
    )


@pytest.mark.parametrize("signal_tier", ["high", SignalTier.HIGH])
def test_answer_with_valid_signal_tier_coerces_to_enum(signal_tier: object) -> None:
    answer = _answer(signal_tier)

    assert isinstance(answer.signal_tier, SignalTier)


def test_answer_with_invalid_signal_tier_raises() -> None:
    with pytest.raises(ValidationError):
        _answer("not_a_tier")
