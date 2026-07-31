"""Tests for money_pit.pipeline.analysis — the A4 post-processor node over the §6.5 container.

Pins wave S3 / ADR 0005 plus the reworked deterministic sizing loop:
- verified = (disposition == SUPPORTED) threads into sizing (supported sizes strictly larger);
- regime is classified only from the deterministic macro indicators, never the container's macro_read;
- container.halt drives ANALYSIS_HALT with an empty action_steps.json;
- empty theses with no halt drives NO_ACTION;
- analysis.md is rendered deterministically from the container, carrying every dropped claim;
- exposure-increasing theses size in priority order against running per-sector, cash, and
  candidate-vs-holdings overlap tallies; exposure-reducing theses are never clamped and never accrue;
- make_analysis_node resolves only the distinct BUY/ADD instruments into candidate_facts.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from pytest import FixtureRequest

from money_pit.compute.regime import classify_regime
from money_pit.compute.sizing import kelly_target_dollars
from money_pit.config import Config
from money_pit.pipeline.analysis import _conviction_rank
from money_pit.pipeline.analysis import _extract_macro_indicators
from money_pit.pipeline.analysis import _held_correlated_value
from money_pit.pipeline.analysis import _in_run_correlated_dollars
from money_pit.pipeline.analysis import _materialize_action_steps
from money_pit.pipeline.analysis import _priority_ordered
from money_pit.pipeline.analysis import _sector_key
from money_pit.pipeline.analysis import _seed_sector_exposure
from money_pit.pipeline.analysis import _to_scenario_list
from money_pit.pipeline.analysis import make_analysis_node
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.analysis_draft import AnalysisHalt
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.analysis_draft import DroppedClaim
from money_pit.schemas.analysis_draft import MacroIndicatorReading
from money_pit.schemas.analysis_draft import Scenario
from money_pit.schemas.analysis_draft import ScenarioTable
from money_pit.schemas.analysis_draft import ThesisJudgment
from money_pit.schemas.answers import Answer
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import ActionType
from money_pit.schemas.enums import Confidence
from money_pit.schemas.enums import ConvictionLevel
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import RegimeTag
from money_pit.schemas.enums import Step1Disposition
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.instrument import InstrumentFacts
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.portfolio import Position
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


def _weak_scenario_table() -> ScenarioTable:
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
        bull=_scenario(10, 0.02),
        base=_scenario(50, 0.0),
        bear=_scenario(40, -0.05),
    )


def _thesis(
    instrument: str,
    disposition: Step1Disposition,
    *,
    claim_id: str = "c-1",
    action_type: ActionType = ActionType.BUY,
    conviction: ConvictionLevel = ConvictionLevel.MEDIUM,
    expected_value: float = 0.08,
    scenario_table: ScenarioTable | None = None,
) -> ThesisJudgment:
    return ThesisJudgment(
        claim_id=claim_id,
        instrument=instrument,
        action_type=action_type,
        description=f"Establish a starter position in {instrument}.",
        group_id=None,
        one_sentence_thesis="The growth thesis still holds on current data.",
        expected_value=expected_value,
        conviction=conviction,
        scenario_table=scenario_table if scenario_table is not None else _scenario_table(),
        invalidation_conditions=[],
        sizing_rationale="Sized to conviction and account risk budget.",
        disposition=disposition,
    )


def _facts(sector: str, *, is_etf: bool = False, holdings: list[str] | None = None) -> InstrumentFacts:
    return InstrumentFacts(sector=sector, is_etf=is_etf, holdings=holdings or [])


class _ScriptedResolver:
    """A ResolveInstrumentFacts fake that records requested tickers and returns canned facts."""

    def __init__(self, facts_by_ticker: dict[str, InstrumentFacts], default: InstrumentFacts) -> None:
        self._facts_by_ticker = facts_by_ticker
        self._default = default
        self.requested: list[str] = []

    def __call__(self, ticker: str) -> InstrumentFacts:
        self.requested.append(ticker)
        return self._facts_by_ticker.get(ticker, self._default)


def _default_resolver() -> _ScriptedResolver:
    return _ScriptedResolver({}, _facts("generic"))


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


def _raising_agent() -> Callable[..., AnalysisJudgment]:
    def _agent(
        _signals: AggregatedSignals,
        _portfolio: PortfolioSnapshot,
        _answers: InitialAnswers,
    ) -> AnalysisJudgment:
        raise RuntimeError("A4 thesis judgment agent boom")

    return _agent


def _config(**overrides: object) -> Config:
    defaults: dict[str, object] = {
        "alpaca_service": "stub",
        "alpaca_username": "stub",
        "alpaca_paper": True,
        "max_position_weight": 1.0,
        "sector_cap": 1.0,
        "overlap_limit": 1.0,
        "cash_min": 0.0,
    }
    return Config(**{**defaults, **overrides})


def _position(ticker: str, current_value: float, *, sector: str = "technology", quantity: float = 1.0) -> Position:
    return Position(
        ticker=ticker,
        quantity=quantity,
        cost_basis=current_value,
        current_value=current_value,
        unrealized_pl=0.0,
        sector=sector,
        factor_tags=[],
    )


def _portfolio(
    *,
    total_account_value: float,
    available_cash: float,
    positions: list[Position] | None = None,
    etf_holdings: dict[str, list[str]] | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        slug=_SLUG,
        as_of="2026-07-02T00:00:00Z",
        total_account_value=total_account_value,
        available_cash=available_cash,
        positions=positions or [],
        sector_weights={},
        correlated_overlaps=[],
        etf_holdings=etf_holdings or {},
    )


def _materialize(
    container: AnalysisJudgment,
    config: Config,
    portfolio: PortfolioSnapshot,
    candidate_facts: dict[str, InstrumentFacts],
    *,
    regime_tag: RegimeTag = RegimeTag.GROWTH_ACCELERATING,
) -> list[ActionStep]:
    return _materialize_action_steps(container, config, portfolio, regime_tag, _SLUG, candidate_facts)


@pytest.fixture
def config() -> Config:
    return _config()


@pytest.fixture
def analysis_working_dir__macro_answers(request: FixtureRequest) -> list[Answer]:
    return getattr(request, "param", [])


@pytest.fixture
def analysis_working_dir__portfolio(request: FixtureRequest) -> PortfolioSnapshot:
    return getattr(request, "param", _portfolio(total_account_value=100000.0, available_cash=100000.0))


@pytest.fixture
def analysis_working_dir(
    tmp_path: Path,
    analysis_working_dir__macro_answers: list[Answer],
    analysis_working_dir__portfolio: PortfolioSnapshot,
) -> Path:
    aggregated_signals = AggregatedSignals(
        slug=_SLUG,
        sources=[],
        claims=[],
        corroborations=[],
        conflicts=[],
        has_actionable_content=True,
    )
    initial_answers = InitialAnswers(slug=_SLUG, sources=[], answers=analysis_working_dir__macro_answers)
    _ = (tmp_path / "aggregated_signals.json").write_text(
        aggregated_signals.model_dump_json(indent=2), encoding="utf-8"
    )
    _ = (tmp_path / "portfolio_snapshot.json").write_text(
        analysis_working_dir__portfolio.model_dump_json(indent=2), encoding="utf-8"
    )
    _ = (tmp_path / "initial_answers.json").write_text(initial_answers.model_dump_json(indent=2), encoding="utf-8")
    return tmp_path


def _run_node(
    config: Config,
    container: AnalysisJudgment,
    working_dir: Path,
    resolver: _ScriptedResolver | None = None,
) -> dict[str, object]:
    node = make_analysis_node(config, _stub_agent(container), resolver or _default_resolver())
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


@pytest.mark.parametrize(
    ("conviction", "expected_rank"),
    [
        (ConvictionLevel.HIGH, 0),
        (ConvictionLevel.MEDIUM, 1),
        (ConvictionLevel.LOW, 2),
    ],
)
def test__conviction_rank_with_each_level_returns_expected_rank(
    conviction: ConvictionLevel, expected_rank: int
) -> None:
    assert _conviction_rank(conviction) == expected_rank


def test__priority_ordered_with_mixed_convictions_sorts_high_before_medium_before_low() -> None:
    theses = [
        _thesis("LOW", Step1Disposition.SUPPORTED, claim_id="c-low", conviction=ConvictionLevel.LOW),
        _thesis("HIGH", Step1Disposition.SUPPORTED, claim_id="c-high", conviction=ConvictionLevel.HIGH),
        _thesis("MED", Step1Disposition.SUPPORTED, claim_id="c-med", conviction=ConvictionLevel.MEDIUM),
    ]
    ordered = _priority_ordered(theses)
    assert [thesis.instrument for _index, thesis in ordered] == ["HIGH", "MED", "LOW"]


def test__priority_ordered_with_equal_conviction_sorts_higher_expected_value_first() -> None:
    theses = [
        _thesis("LOWEV", Step1Disposition.SUPPORTED, claim_id="c-1", expected_value=0.05),
        _thesis("HIGHEV", Step1Disposition.SUPPORTED, claim_id="c-2", expected_value=0.10),
    ]
    ordered = _priority_ordered(theses)
    assert [thesis.instrument for _index, thesis in ordered] == ["HIGHEV", "LOWEV"]


def test__priority_ordered_with_equal_conviction_and_ev_is_stable_by_original_index() -> None:
    theses = [
        _thesis("FIRST", Step1Disposition.SUPPORTED, claim_id="c-1"),
        _thesis("SECOND", Step1Disposition.SUPPORTED, claim_id="c-2"),
    ]
    ordered = _priority_ordered(theses)
    assert [index for index, _thesis in ordered] == [0, 1]


@pytest.mark.parametrize(
    ("sector", "expected"),
    [("Technology", "technology"), ("ENERGY", "energy"), ("energy", "energy")],
)
def test__sector_key_with_mixed_casing_casefolds(sector: str, expected: str) -> None:
    assert _sector_key(sector) == expected


def test__seed_sector_exposure_with_same_sector_different_casing_folds_and_sums() -> None:
    positions = [
        _position("AAA", 1000.0, sector="Technology"),
        _position("BBB", 2000.0, sector="technology"),
    ]
    assert _seed_sector_exposure(positions) == {"technology": pytest.approx(3000.0)}


def test__held_correlated_value_with_candidate_etf_holding_held_name_counts_it() -> None:
    facts = _facts("semis", is_etf=True, holdings=["NVDA"])
    snapshot = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", 10000.0)],
    )
    assert _held_correlated_value("SMH", facts, snapshot) == pytest.approx(10000.0)


def test__held_correlated_value_with_candidate_held_by_held_etf_counts_it() -> None:
    facts = _facts("semis")
    snapshot = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("SMH", 5000.0)],
        etf_holdings={"SMH": ["NVDA"]},
    )
    assert _held_correlated_value("NVDA", facts, snapshot) == pytest.approx(5000.0)


def test__held_correlated_value_excludes_the_instruments_own_held_value() -> None:
    facts = _facts("semis")
    snapshot = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", 10000.0)],
    )
    assert _held_correlated_value("NVDA", facts, snapshot) == 0.0


def test__held_correlated_value_is_case_insensitive() -> None:
    facts = _facts("semis", is_etf=True, holdings=["nvda"])
    snapshot = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", 10000.0)],
    )
    assert _held_correlated_value("smh", facts, snapshot) == pytest.approx(10000.0)


def test__held_correlated_value_with_uncorrelated_candidate_returns_zero() -> None:
    facts = _facts("semis")
    snapshot = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", 10000.0)],
    )
    assert _held_correlated_value("AMD", facts, snapshot) == 0.0


def test__in_run_correlated_dollars_with_prior_held_by_candidate_counts_it() -> None:
    facts = _facts("semis", is_etf=True, holdings=["NVDA"])
    candidate_facts = {"SMH": facts, "NVDA": _facts("semis")}
    assert _in_run_correlated_dollars("SMH", facts, candidate_facts, {"NVDA": 5000.0}) == pytest.approx(5000.0)


def test__in_run_correlated_dollars_with_candidate_held_by_prior_counts_it() -> None:
    facts = _facts("semis")
    candidate_facts = {"NVDA": facts, "SMH": _facts("semis", is_etf=True, holdings=["NVDA"])}
    assert _in_run_correlated_dollars("NVDA", facts, candidate_facts, {"SMH": 7000.0}) == pytest.approx(7000.0)


def test__in_run_correlated_dollars_with_unrelated_prior_returns_zero() -> None:
    facts = _facts("semis")
    candidate_facts = {"NVDA": facts, "AMD": _facts("semis")}
    assert _in_run_correlated_dollars("NVDA", facts, candidate_facts, {"AMD": 3000.0}) == 0.0


def test__in_run_correlated_dollars_excludes_the_instrument_itself() -> None:
    facts = _facts("semis")
    candidate_facts = {"NVDA": facts}
    assert _in_run_correlated_dollars("NVDA", facts, candidate_facts, {"NVDA": 9000.0}) == 0.0


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
    supported_notional = steps["NVDA"].execution_parameters.notional
    unverified_notional = steps["AMD"].execution_parameters.notional
    assert supported_notional is not None
    assert unverified_notional is not None
    assert float(supported_notional) > float(unverified_notional)


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


def test__materialize_action_steps_with_sized_theses_assigns_ordered_step_ids() -> None:
    config = _config()
    portfolio = _portfolio(total_account_value=100000.0, available_cash=100000.0)
    container = AnalysisJudgment(
        theses=[
            _thesis("NVDA", Step1Disposition.SUPPORTED, claim_id="c-1"),
            _thesis("AMD", Step1Disposition.SUPPORTED, claim_id="c-2"),
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"NVDA": _facts("semis"), "AMD": _facts("software")}
    steps = _materialize(container, config, portfolio, candidate_facts)
    assert [step.step_id for step in steps] == ["A001", "A002"]


def test__materialize_action_steps_with_ev_below_gate_yields_no_action_steps() -> None:
    config = _config(ev_gate=1.0)
    portfolio = _portfolio(total_account_value=100000.0, available_cash=100000.0)
    container = AnalysisJudgment(
        theses=[
            _thesis("NVDA", Step1Disposition.SUPPORTED, claim_id="c-1"),
            _thesis("AMD", Step1Disposition.SUPPORTED, claim_id="c-2"),
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"NVDA": _facts("semis"), "AMD": _facts("software")}
    steps = _materialize(container, config, portfolio, candidate_facts)
    assert steps == []


def test__materialize_action_steps_with_running_sector_cap_clamps_the_second_same_sector_buy() -> None:
    config = _config(sector_cap=0.30)
    portfolio = _portfolio(total_account_value=100000.0, available_cash=100000.0)
    container = AnalysisJudgment(
        theses=[
            _thesis("AAA", Step1Disposition.SUPPORTED, claim_id="c-1"),
            _thesis("BBB", Step1Disposition.SUPPORTED, claim_id="c-2"),
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"AAA": _facts("semis"), "BBB": _facts("semis")}
    steps = {step.instrument: step for step in _materialize(container, config, portfolio, candidate_facts)}
    assert float(steps["AAA"].execution_parameters.notional or "nan") == pytest.approx(25000.0)
    assert float(steps["BBB"].execution_parameters.notional or "nan") == pytest.approx(5000.0)


def test__materialize_action_steps_with_different_sectors_are_each_unclamped_by_sector() -> None:
    config = _config(sector_cap=0.30)
    portfolio = _portfolio(total_account_value=100000.0, available_cash=100000.0)
    container = AnalysisJudgment(
        theses=[
            _thesis("AAA", Step1Disposition.SUPPORTED, claim_id="c-1"),
            _thesis("BBB", Step1Disposition.SUPPORTED, claim_id="c-2"),
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"AAA": _facts("semis"), "BBB": _facts("software")}
    steps = {step.instrument: step for step in _materialize(container, config, portfolio, candidate_facts)}
    assert float(steps["AAA"].execution_parameters.notional or "nan") == pytest.approx(25000.0)
    assert float(steps["BBB"].execution_parameters.notional or "nan") == pytest.approx(25000.0)


def test__materialize_action_steps_with_running_cash_clamps_the_second_buy() -> None:
    config = _config()
    portfolio = _portfolio(total_account_value=100000.0, available_cash=40000.0)
    container = AnalysisJudgment(
        theses=[
            _thesis("AAA", Step1Disposition.SUPPORTED, claim_id="c-1"),
            _thesis("BBB", Step1Disposition.SUPPORTED, claim_id="c-2"),
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"AAA": _facts("semis"), "BBB": _facts("software")}
    steps = {step.instrument: step for step in _materialize(container, config, portfolio, candidate_facts)}
    first = float(steps["AAA"].execution_parameters.notional or "nan")
    second = float(steps["BBB"].execution_parameters.notional or "nan")
    assert first == pytest.approx(25000.0)
    assert second == pytest.approx(15000.0)
    assert second < first


def test__materialize_action_steps_drops_sub_minimum_notional_candidate() -> None:
    config = _config()
    portfolio = _portfolio(total_account_value=100000.0, available_cash=0.005)
    container = AnalysisJudgment(
        theses=[_thesis("NVDA", Step1Disposition.SUPPORTED, claim_id="c-1")],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"NVDA": _facts("semis")}
    steps = _materialize(container, config, portfolio, candidate_facts)
    assert steps == []


def test__materialize_action_steps_with_candidate_etf_holding_held_name_is_reduced() -> None:
    config = _config(overlap_limit=0.30)
    portfolio = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", 10000.0)],
    )
    container = AnalysisJudgment(
        theses=[_thesis("SMH", Step1Disposition.SUPPORTED, claim_id="c-1")],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"SMH": _facts("semis", is_etf=True, holdings=["NVDA"])}
    steps = _materialize(container, config, portfolio, candidate_facts)
    assert float(steps[0].execution_parameters.notional or "nan") == pytest.approx(20000.0)


def test__materialize_action_steps_with_candidate_held_by_held_etf_is_reduced() -> None:
    config = _config(overlap_limit=0.30)
    portfolio = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("SMH", 10000.0)],
        etf_holdings={"SMH": ["NVDA"]},
    )
    container = AnalysisJudgment(
        theses=[_thesis("NVDA", Step1Disposition.SUPPORTED, claim_id="c-1")],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"NVDA": _facts("semis")}
    steps = _materialize(container, config, portfolio, candidate_facts)
    assert float(steps[0].execution_parameters.notional or "nan") == pytest.approx(20000.0)


def test__materialize_action_steps_with_non_overlapping_candidate_is_not_reduced() -> None:
    config = _config(overlap_limit=0.30)
    portfolio = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", 10000.0)],
    )
    container = AnalysisJudgment(
        theses=[_thesis("AMD", Step1Disposition.SUPPORTED, claim_id="c-1")],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"AMD": _facts("semis")}
    steps = _materialize(container, config, portfolio, candidate_facts)
    assert float(steps[0].execution_parameters.notional or "nan") == pytest.approx(25000.0)


def test__materialize_action_steps_with_priority_sizes_high_conviction_before_low_same_sector() -> None:
    config = _config(sector_cap=0.20)
    portfolio = _portfolio(total_account_value=100000.0, available_cash=100000.0)
    container = AnalysisJudgment(
        theses=[
            _thesis("LOW", Step1Disposition.SUPPORTED, claim_id="c-low", conviction=ConvictionLevel.LOW),
            _thesis("HIGH", Step1Disposition.SUPPORTED, claim_id="c-high", conviction=ConvictionLevel.HIGH),
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    candidate_facts = {"LOW": _facts("semis"), "HIGH": _facts("semis")}
    steps = _materialize(container, config, portfolio, candidate_facts)
    assert [step.instrument for step in steps] == ["HIGH"]
    assert steps[0].step_id == "A001"
    assert float(steps[0].execution_parameters.notional or "nan") == pytest.approx(20000.0)


def test__materialize_action_steps_with_sell_full_exits_by_held_quantity() -> None:
    config = _config()
    portfolio = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", 40000.0, quantity=40.0)],
    )
    container = AnalysisJudgment(
        theses=[_thesis("NVDA", Step1Disposition.SUPPORTED, claim_id="c-1", action_type=ActionType.SELL)],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    steps = _materialize(container, config, portfolio, {})
    assert len(steps) == 1
    params = steps[0].execution_parameters
    assert params.qty == "40"
    assert params.notional is None
    assert params.side == "sell"


def test__materialize_action_steps_with_sell_below_ev_gate_still_exits() -> None:
    config = _config()
    portfolio = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", 40000.0, quantity=40.0)],
    )
    container = AnalysisJudgment(
        theses=[
            _thesis(
                "NVDA",
                Step1Disposition.SUPPORTED,
                claim_id="c-1",
                action_type=ActionType.SELL,
                scenario_table=_weak_scenario_table(),
            )
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    steps = _materialize(container, config, portfolio, {})
    assert len(steps) == 1
    params = steps[0].execution_parameters
    assert params.qty == "40"
    assert params.notional is None
    assert params.side == "sell"


def test__materialize_action_steps_with_sell_of_non_held_ticker_is_dropped() -> None:
    config = _config()
    portfolio = _portfolio(total_account_value=100000.0, available_cash=100000.0)
    container = AnalysisJudgment(
        theses=[_thesis("GHOST", Step1Disposition.SUPPORTED, claim_id="c-1", action_type=ActionType.SELL)],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    assert _materialize(container, config, portfolio, {}) == []


def test__materialize_action_steps_with_trim_reduces_by_current_value_minus_kelly_target() -> None:
    config = _config()
    current_value = 50000.0
    portfolio = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", current_value, quantity=100.0)],
    )
    thesis = _thesis("NVDA", Step1Disposition.SUPPORTED, claim_id="c-1", action_type=ActionType.TRIM)
    container = AnalysisJudgment(theses=[thesis], dropped_claims=[], macro_read=[], halt=None)
    target = kelly_target_dollars(
        _to_scenario_list(thesis.scenario_table),
        portfolio.total_account_value,
        config,
        verified=True,
        regime_uncertain=False,
    )
    steps = _materialize(container, config, portfolio, {})
    assert len(steps) == 1
    params = steps[0].execution_parameters
    assert params.notional == f"{current_value - target:.2f}"
    assert params.qty is None
    assert params.side == "sell"


def test__materialize_action_steps_with_trim_already_at_target_is_dropped() -> None:
    config = _config()
    thesis = _thesis("NVDA", Step1Disposition.SUPPORTED, claim_id="c-1", action_type=ActionType.TRIM)
    target = kelly_target_dollars(
        _to_scenario_list(thesis.scenario_table),
        100000.0,
        config,
        verified=True,
        regime_uncertain=False,
    )
    portfolio = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("NVDA", target, quantity=100.0)],
    )
    container = AnalysisJudgment(theses=[thesis], dropped_claims=[], macro_read=[], halt=None)
    assert _materialize(container, config, portfolio, {}) == []


def test__materialize_action_steps_with_trim_of_non_held_ticker_is_dropped() -> None:
    config = _config()
    portfolio = _portfolio(total_account_value=100000.0, available_cash=100000.0)
    container = AnalysisJudgment(
        theses=[_thesis("GHOST", Step1Disposition.SUPPORTED, claim_id="c-1", action_type=ActionType.TRIM)],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    assert _materialize(container, config, portfolio, {}) == []


@pytest.mark.parametrize("action_type", [ActionType.SELL, ActionType.TRIM])
def test__materialize_action_steps_with_exposure_reducing_thesis_does_not_consume_buy_budget(
    action_type: ActionType,
) -> None:
    config = _config(sector_cap=0.25)
    portfolio = _portfolio(
        total_account_value=100000.0,
        available_cash=100000.0,
        positions=[_position("XLE", 50000.0, sector="energy", quantity=500.0)],
    )
    buy_only = AnalysisJudgment(
        theses=[_thesis("AMD", Step1Disposition.SUPPORTED, claim_id="c-buy")],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    baseline = _materialize(buy_only, config, portfolio, {"AMD": _facts("semis")})
    baseline_amd = baseline[0].execution_parameters.notional

    with_exit = AnalysisJudgment(
        theses=[
            _thesis("XLE", Step1Disposition.SUPPORTED, claim_id="c-exit", action_type=action_type),
            _thesis("AMD", Step1Disposition.SUPPORTED, claim_id="c-buy"),
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    steps = {step.instrument: step for step in _materialize(with_exit, config, portfolio, {"AMD": _facts("semis")})}
    assert steps["AMD"].execution_parameters.notional == baseline_amd


@pytest.mark.parametrize("analysis_working_dir__macro_answers", [_GROWTH_ACCELERATING_ANSWERS], indirect=True)
@pytest.mark.parametrize(
    "analysis_working_dir__portfolio",
    [
        _portfolio(
            total_account_value=100000.0,
            available_cash=100000.0,
            positions=[_position("NVDA", 10000.0)],
        )
    ],
    indirect=True,
)
def test_make_analysis_node_resolves_only_distinct_buy_add_and_reflects_overlap_clamp(
    analysis_working_dir: Path,
) -> None:
    container = AnalysisJudgment(
        theses=[
            _thesis("SMH", Step1Disposition.SUPPORTED, claim_id="c-buy"),
            _thesis("NVDA", Step1Disposition.SUPPORTED, claim_id="c-sell", action_type=ActionType.SELL),
            _thesis("SMH", Step1Disposition.SUPPORTED, claim_id="c-add", action_type=ActionType.ADD),
        ],
        dropped_claims=[],
        macro_read=[],
        halt=None,
    )
    resolver = _ScriptedResolver({"SMH": _facts("semis", is_etf=True, holdings=["NVDA"])}, _facts("generic"))
    _ = _run_node(_config(overlap_limit=0.30), container, analysis_working_dir, resolver)

    assert resolver.requested == ["SMH"]
    steps = _read_action_steps(analysis_working_dir)
    buy_smh = next(s for s in steps if s.instrument == "SMH" and s.action_type == ActionType.BUY)
    sell_nvda = next(s for s in steps if s.instrument == "NVDA" and s.action_type == ActionType.SELL)
    assert float(buy_smh.execution_parameters.notional or "nan") == pytest.approx(20000.0)
    assert sell_nvda.execution_parameters.qty == "1"
    assert sell_nvda.execution_parameters.notional is None
    assert sell_nvda.execution_parameters.side == "sell"


def test_make_analysis_node_with_agent_exception_sets_analysis_halt(config: Config, analysis_working_dir: Path) -> None:
    node = make_analysis_node(config, _raising_agent(), _default_resolver())
    result = node({"slug": _SLUG, "working_dir": str(analysis_working_dir)})
    assert result.get("terminal_state") == TerminalState.ANALYSIS_HALT


def test_make_analysis_node_with_agent_exception_writes_empty_action_steps(
    config: Config, analysis_working_dir: Path
) -> None:
    node = make_analysis_node(config, _raising_agent(), _default_resolver())
    _ = node({"slug": _SLUG, "working_dir": str(analysis_working_dir)})
    assert _read_action_steps(analysis_working_dir) == []
