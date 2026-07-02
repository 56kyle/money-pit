"""Tests for money_pit.schemas.analysis_draft — the §6.5 AnalysisJudgment container and its members.

Pins wave S3 / ADR 0005: ThesisJudgment (all fields required except group_id), DroppedClaim,
MacroIndicatorReading, AnalysisHalt (pure data, no TerminalState), and the container round-trip.
"""
import pytest
from pydantic import ValidationError

from money_pit.schemas.analysis_draft import (
    AnalysisHalt,
    AnalysisJudgment,
    DroppedClaim,
    MacroIndicatorReading,
    Scenario,
    ScenarioTable,
    ThesisJudgment,
)
from money_pit.schemas.enums import ActionType, ConvictionLevel, Step1Disposition


def _scenario_table() -> ScenarioTable:
    def _scenario(probability: int, return_pct: float) -> Scenario:
        return Scenario(
            probability=probability,
            return_pct=return_pct,
            timeframe=None,
            confirming_metric=None,
            mechanism=None,
            max_drawdown=None,
        )

    return ScenarioTable(
        bull=_scenario(30, 0.20),
        base=_scenario(50, 0.08),
        bear=_scenario(20, -0.10),
    )


def _thesis_kwargs() -> dict[str, object]:
    return {
        "claim_id": "c-1",
        "instrument": "NVDA",
        "action_type": ActionType.BUY,
        "description": "Establish a starter position.",
        "group_id": None,
        "one_sentence_thesis": "The growth thesis still holds on current data.",
        "expected_value": 0.08,
        "conviction": ConvictionLevel.MEDIUM,
        "scenario_table": _scenario_table(),
        "invalidation_conditions": [],
        "sizing_rationale": "Sized to conviction and account risk budget.",
        "disposition": Step1Disposition.SUPPORTED,
    }


_THESIS_REQUIRED_FIELDS: list[str] = [
    "claim_id",
    "instrument",
    "action_type",
    "description",
    "one_sentence_thesis",
    "expected_value",
    "conviction",
    "scenario_table",
    "invalidation_conditions",
    "sizing_rationale",
    "disposition",
]


@pytest.mark.parametrize("field", _THESIS_REQUIRED_FIELDS)
def test_thesis_judgment_rejects_missing_required_field(field: str) -> None:
    kwargs = _thesis_kwargs()
    del kwargs[field]
    with pytest.raises(ValidationError):
        _ = ThesisJudgment(**kwargs)


def test_analysis_halt_rejects_terminal_state() -> None:
    with pytest.raises(ValidationError):
        _ = AnalysisHalt(reason="A4 could not resolve dispositions.", terminal_state="ANALYSIS_HALT")


def test_analysis_judgment_round_trips() -> None:
    container = AnalysisJudgment(
        theses=[ThesisJudgment(**_thesis_kwargs())],
        dropped_claims=[DroppedClaim(claim_id="c-2", reason="Contradicted by the macro read.")],
        macro_read=[MacroIndicatorReading(indicator="pmi", reading="55.0, expanding", favorable=True)],
        halt=None,
    )
    assert AnalysisJudgment.model_validate_json(container.model_dump_json()) == container
