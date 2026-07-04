"""Tests for money_pit.pipeline.analysis — the A4 post-processor node over the §6.5 container.

Pins wave S3 / ADR 0005:
- verified = (disposition == SUPPORTED) threads into sizing (supported sizes strictly larger);
- regime is classified only from the deterministic macro indicators, never the container's macro_read;
- container.halt drives ANALYSIS_HALT with an empty action_steps.json;
- empty theses with no halt drives NO_ACTION;
- analysis.md is rendered deterministically from the container, carrying every dropped claim.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from pytest import FixtureRequest

from money_pit.compute.regime import classify_regime
from money_pit.config import Config
from money_pit.pipeline.analysis import _extract_macro_indicators, make_analysis_node
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.analysis_draft import (
    AnalysisHalt,
    AnalysisJudgment,
    DroppedClaim,
    MacroIndicatorReading,
    Scenario,
    ScenarioTable,
    ThesisJudgment,
)
from money_pit.schemas.answers import Answer, InitialAnswers
from money_pit.schemas.enums import (
    ActionType,
    Confidence,
    ConvictionLevel,
    QuestionCategory,
    Step1Disposition,
    TerminalState,
)
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.questions import INDICATOR_PREFIX
from money_pit.schemas.signals import AggregatedSignals

_SLUG = "test-run"
_ACTION_STEPS_ADAPTER: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])


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


def _thesis(instrument: str, disposition: Step1Disposition, *, claim_id: str = "c-1") -> ThesisJudgment:
    return ThesisJudgment(
        claim_id=claim_id,
        instrument=instrument,
        action_type=ActionType.BUY,
        description=f"Establish a starter position in {instrument}.",
        group_id=None,
        one_sentence_thesis="The growth thesis still holds on current data.",
        expected_value=0.08,
        conviction=ConvictionLevel.MEDIUM,
        scenario_table=_scenario_table(),
        invalidation_conditions=[],
        sizing_rationale="Sized to conviction and account risk budget.",
        disposition=disposition,
    )


def _macro_answer(indicator: str, value: float) -> Answer:
    return Answer(
        question_id=f"Q-{indicator}",
        question=f"What is the current {indicator} reading?",
        category=QuestionCategory.MACRO_REGIME,
        signal_source=f"{INDICATOR_PREFIX}{indicator}",
        signal_tier="high",
        answer=f"{indicator} is at {value}.",
        confidence=Confidence.HIGH,
        sources_used=[],
        data_retrieved={"value": value},
        limitations="none",
    )


def _answer(
    *,
    signal_source: str,
    data_retrieved: dict[str, object] | None,
    category: QuestionCategory = QuestionCategory.MACRO_REGIME,
) -> Answer:
    return Answer(
        question_id="Q-1",
        question="What is the current reading?",
        category=category,
        signal_source=signal_source,
        signal_tier="high",
        answer="A synthesized answer.",
        confidence=Confidence.HIGH,
        sources_used=[],
        data_retrieved=data_retrieved,
        limitations="none",
    )


_INDICATOR_FIELDS: list[str] = [
    "yield_curve",
    "credit_spreads",
    "pmi",
    "earnings_revisions",
    "inflation",
]


_GROWTH_ACCELERATING_ANSWERS: list[Answer] = [
    _macro_answer("yield_curve", 1.0),
    _macro_answer("credit_spreads", 2.0),
    _macro_answer("pmi", 55.0),
    _macro_answer("earnings_revisions", 1.0),
    _macro_answer("inflation", 1.5),
]


def _stub_agent(container: AnalysisJudgment) -> Callable[..., AnalysisJudgment]:
    def _agent(
        _signals: AggregatedSignals,
        _portfolio: PortfolioSnapshot,
        _answers: InitialAnswers,
    ) -> AnalysisJudgment:
        return container

    return _agent


@pytest.fixture
def config() -> Config:
    return Config(
        alpaca_service="stub",
        alpaca_username="stub",
        max_position_weight=1.0,
        sector_cap=1.0,
        overlap_limit=1.0,
        cash_min=0.0,
    )


@pytest.fixture
def analysis_working_dir__macro_answers(request: FixtureRequest) -> list[Answer]:
    return getattr(request, "param", [])


@pytest.fixture
def analysis_working_dir(tmp_path: Path, analysis_working_dir__macro_answers: list[Answer]) -> Path:
    aggregated_signals = AggregatedSignals(
        slug=_SLUG,
        sources=[],
        claims=[],
        corroborations=[],
        conflicts=[],
        has_actionable_content=True,
    )
    portfolio_snapshot = PortfolioSnapshot(
        slug=_SLUG,
        as_of="2026-07-02T00:00:00Z",
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[],
        sector_weights={},
        correlated_overlaps=[],
    )
    initial_answers = InitialAnswers(slug=_SLUG, sources=[], answers=analysis_working_dir__macro_answers)
    _ = (tmp_path / "aggregated_signals.json").write_text(
        aggregated_signals.model_dump_json(indent=2), encoding="utf-8"
    )
    _ = (tmp_path / "portfolio_snapshot.json").write_text(
        portfolio_snapshot.model_dump_json(indent=2), encoding="utf-8"
    )
    _ = (tmp_path / "initial_answers.json").write_text(initial_answers.model_dump_json(indent=2), encoding="utf-8")
    return tmp_path


def _run_node(config: Config, container: AnalysisJudgment, working_dir: Path) -> dict[str, object]:
    node = make_analysis_node(config, _stub_agent(container))
    return node({"slug": _SLUG, "working_dir": str(working_dir)})


def _read_action_steps(working_dir: Path) -> list[ActionStep]:
    return _ACTION_STEPS_ADAPTER.validate_json((working_dir / "action_steps.json").read_text(encoding="utf-8"))


def test__extract_macro_indicators_with_valid_answer_populates_indicator() -> None:
    result = _extract_macro_indicators(
        [_answer(signal_source=f"{INDICATOR_PREFIX}pmi", data_retrieved={"value": 55.0})]
    )
    assert result.pmi == 55.0


def test__extract_macro_indicators_with_non_macro_regime_category_skips_it() -> None:
    result = _extract_macro_indicators(
        [
            _answer(
                signal_source=f"{INDICATOR_PREFIX}pmi",
                data_retrieved={"value": 55.0},
                category=QuestionCategory.CURRENT_EVENTS,
            )
        ]
    )
    assert result.pmi is None


def test__extract_macro_indicators_with_non_indicator_signal_source_skips_it() -> None:
    result = _extract_macro_indicators([_answer(signal_source="raw:pmi", data_retrieved={"value": 55.0})])
    assert result.pmi is None


def test__extract_macro_indicators_with_unknown_indicator_name_skips_it() -> None:
    result = _extract_macro_indicators(
        [_answer(signal_source=f"{INDICATOR_PREFIX}gdp", data_retrieved={"value": 55.0})]
    )
    assert all(getattr(result, field) is None for field in _INDICATOR_FIELDS)


@pytest.mark.parametrize("data_retrieved", [{"value": "high"}, {"value": None}, {}, None])
def test__extract_macro_indicators_with_non_numeric_value_leaves_none(
    data_retrieved: dict[str, object] | None,
) -> None:
    result = _extract_macro_indicators([_answer(signal_source=f"{INDICATOR_PREFIX}pmi", data_retrieved=data_retrieved)])
    assert result.pmi is None


@pytest.mark.parametrize("indicator", _INDICATOR_FIELDS)
def test__extract_macro_indicators_with_absent_indicator_leaves_none(indicator: str) -> None:
    result = _extract_macro_indicators([])
    assert getattr(result, indicator) is None


def test_make_analysis_node_with_disposition_sizes_supported_larger(config: Config, analysis_working_dir: Path) -> None:
    container = AnalysisJudgment(
        theses=[
            _thesis("NVDA", Step1Disposition.SUPPORTED, claim_id="c-supported"),
            _thesis("AMD", Step1Disposition.UNVERIFIED, claim_id="c-unverified"),
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    _ = _run_node(config, container, analysis_working_dir)
    steps = {step.instrument: step for step in _read_action_steps(analysis_working_dir)}
    assert steps["NVDA"].execution_parameters.notional > steps["AMD"].execution_parameters.notional


@pytest.mark.parametrize("analysis_working_dir__macro_answers", [_GROWTH_ACCELERATING_ANSWERS], indirect=True)
def test_make_analysis_node_with_contradictory_macro_read_ignores_it_for_regime(
    config: Config, analysis_working_dir: Path
) -> None:
    container = AnalysisJudgment(
        theses=[_thesis("NVDA", Step1Disposition.SUPPORTED)],
        dropped_claims=[],
        macro_read=[
            MacroIndicatorReading(indicator="pmi", reading="deeply contractionary", favorable=False),
            MacroIndicatorReading(indicator="credit_spreads", reading="blowing out", favorable=False),
            MacroIndicatorReading(indicator="yield_curve", reading="inverted", favorable=False),
        ],
        halt=None,
    )
    initial_answers = InitialAnswers(slug=_SLUG, sources=[], answers=_GROWTH_ACCELERATING_ANSWERS)
    expected_regime = classify_regime(_extract_macro_indicators(initial_answers.answers), config)

    _ = _run_node(config, container, analysis_working_dir)
    steps = _read_action_steps(analysis_working_dir)
    assert steps[0].regime_tag == expected_regime


def test_make_analysis_node_with_halt_sets_analysis_halt(config: Config, analysis_working_dir: Path) -> None:
    container = AnalysisJudgment(
        theses=[],
        dropped_claims=[],
        macro_read=[],
        halt=AnalysisHalt(reason="A4 could not resolve claim dispositions."),
    )
    result = _run_node(config, container, analysis_working_dir)
    assert result.get("terminal_state") == TerminalState.ANALYSIS_HALT


def test_make_analysis_node_with_halt_writes_empty_action_steps(config: Config, analysis_working_dir: Path) -> None:
    container = AnalysisJudgment(
        theses=[],
        dropped_claims=[],
        macro_read=[],
        halt=AnalysisHalt(reason="A4 could not resolve claim dispositions."),
    )
    _ = _run_node(config, container, analysis_working_dir)
    assert _read_action_steps(analysis_working_dir) == []


def test_make_analysis_node_with_empty_theses_sets_no_action(config: Config, analysis_working_dir: Path) -> None:
    container = AnalysisJudgment(theses=[], dropped_claims=[], macro_read=[], halt=None)
    result = _run_node(config, container, analysis_working_dir)
    assert result.get("terminal_state") == TerminalState.NO_ACTION


def test_make_analysis_node_with_dropped_claim_renders_it_in_analysis_md(
    config: Config, analysis_working_dir: Path
) -> None:
    dropped = DroppedClaim(claim_id="c-dropped", reason="Contradicted by the fresh earnings print.")
    container = AnalysisJudgment(
        theses=[_thesis("NVDA", Step1Disposition.SUPPORTED)],
        dropped_claims=[dropped],
        macro_read=[],
        halt=None,
    )
    _ = _run_node(config, container, analysis_working_dir)
    rendered = (analysis_working_dir / "analysis.md").read_text(encoding="utf-8")
    assert dropped.claim_id in rendered
    assert dropped.reason in rendered
