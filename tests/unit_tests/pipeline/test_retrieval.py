"""Tests for money_pit.pipeline.retrieval."""

from collections.abc import Callable
from pathlib import Path

import pytest
from pytest import FixtureRequest

from money_pit.agents.research_tools import DeterministicResearchTools
from money_pit.pipeline.retrieval import _deterministic_answer
from money_pit.pipeline.retrieval import _draft_to_answer
from money_pit.pipeline.retrieval import _fetch_deterministic
from money_pit.pipeline.retrieval import make_retrieval_node
from money_pit.schemas.answer_draft import AnswerDraft
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import Confidence
from money_pit.schemas.enums import DataSourceToken
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import SourceType
from money_pit.schemas.fetch_result import FetchError
from money_pit.schemas.fetch_result import FetchResult
from money_pit.schemas.fetch_result import FetchValue
from money_pit.schemas.fetch_result import NoData
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.questions import InitialQuestions
from money_pit.schemas.questions import Question
from money_pit.schemas.questions import SignalSummary
from money_pit.schemas.signals import AggregatedSignals
from tests.unit_tests.pipeline.conftest import CapturedLog


@pytest.fixture
def answer_draft(request: FixtureRequest, answer_draft__sources_used: list[DataSourceToken]) -> AnswerDraft:
    return getattr(
        request,
        "param",
        AnswerDraft(
            question_id="Q001",
            answer="The 10y-2y spread has steepened over the quarter.",
            sources_used=answer_draft__sources_used,
            data_retrieved={"value": 1.23},
            limitations="none",
        ),
    )


@pytest.fixture
def answer_draft__sources_used(request: FixtureRequest) -> list[DataSourceToken]:
    return getattr(request, "param", [DataSourceToken.FRED_MCP])


@pytest.fixture
def question(
    request: FixtureRequest,
    question__category: QuestionCategory,
    question__signal_source: str | None,
    question__signal_tier: str,
) -> Question:
    return getattr(
        request,
        "param",
        Question(
            id="Q001",
            category=question__category,
            question="What is the yield curve signalling?",
            signal_source=question__signal_source,
            signal_tier=question__signal_tier,
            rationale="Macro regime read depends on the term-structure slope.",
            data_sources=[DataSourceToken.FRED_MCP],
            answer=None,
        ),
    )


@pytest.fixture
def question__category(request: FixtureRequest) -> QuestionCategory:
    return getattr(request, "param", QuestionCategory.MACRO_REGIME)


@pytest.fixture
def question__signal_source(request: FixtureRequest) -> str | None:
    return getattr(request, "param", "indicator:yield_curve")


@pytest.fixture
def question__signal_tier(request: FixtureRequest) -> str:
    return getattr(request, "param", "high")


@pytest.mark.parametrize(
    ("answer_draft__sources_used", "expected"),
    [
        ([DataSourceToken.FRED_MCP], Confidence.HIGH),
        ([DataSourceToken.YFINANCE_MCP], Confidence.MEDIUM),
        ([DataSourceToken.BRAVE_SEARCH_MCP], Confidence.LOW),
        ([], Confidence.LOW),
    ],
    indirect=["answer_draft__sources_used"],
)
def test__draft_to_answer_with_confidence(
    answer_draft: AnswerDraft,
    question: Question,
    expected: Confidence,
) -> None:
    result = _draft_to_answer(answer_draft, question)
    assert result.confidence == expected


def test__draft_to_answer_with_category(answer_draft: AnswerDraft, question: Question) -> None:
    result = _draft_to_answer(answer_draft, question)
    assert result.category == question.category


def test__draft_to_answer_with_signal_source(answer_draft: AnswerDraft, question: Question) -> None:
    result = _draft_to_answer(answer_draft, question)
    assert result.signal_source == question.signal_source


def test__draft_to_answer_with_signal_tier(answer_draft: AnswerDraft, question: Question) -> None:
    result = _draft_to_answer(answer_draft, question)
    assert result.signal_tier == question.signal_tier


class _StubResearchTools:
    def __init__(self, *, fred_result: FetchResult, ticker_result: FetchResult) -> None:
        self._fred_result = fred_result
        self._ticker_result = ticker_result

    def fetch_fred_series(self, series_id: str) -> FetchResult:
        return self._fred_result

    def fetch_ticker_price(self, ticker: str) -> FetchResult:
        return self._ticker_result


@pytest.fixture
def deterministic_tools(
    request: FixtureRequest,
    deterministic_tools__fred_result: FetchResult,
    deterministic_tools__ticker_result: FetchResult,
) -> DeterministicResearchTools:
    return getattr(
        request,
        "param",
        _StubResearchTools(
            fred_result=deterministic_tools__fred_result,
            ticker_result=deterministic_tools__ticker_result,
        ),
    )


@pytest.fixture
def deterministic_tools__fred_result(request: FixtureRequest) -> FetchResult:
    return getattr(request, "param", FetchValue(value=1.23))


@pytest.fixture
def deterministic_tools__ticker_result(request: FixtureRequest) -> FetchResult:
    return getattr(request, "param", FetchValue(value=187.5))


@pytest.mark.parametrize(
    (
        "question__category",
        "question__signal_source",
        "deterministic_tools__fred_result",
        "deterministic_tools__ticker_result",
        "expected",
    ),
    [
        (
            QuestionCategory.MACRO_REGIME,
            "indicator:yield_curve",
            FetchValue(value=1.23),
            NoData(),
            FetchValue(value=1.23),
        ),
        (
            QuestionCategory.PORTFOLIO_GAP,
            "AAPL",
            NoData(),
            FetchValue(value=187.5),
            FetchValue(value=187.5),
        ),
    ],
    indirect=[
        "question__category",
        "question__signal_source",
        "deterministic_tools__fred_result",
        "deterministic_tools__ticker_result",
    ],
)
def test__fetch_deterministic_with_fetchable_source_delegates(
    question: Question,
    deterministic_tools: DeterministicResearchTools,
    expected: FetchResult,
) -> None:
    assert _fetch_deterministic(question, deterministic_tools) == expected


@pytest.mark.parametrize(
    ("question__category", "question__signal_source"),
    [
        (QuestionCategory.MACRO_REGIME, None),
        (QuestionCategory.MACRO_REGIME, "indicator:unknown_thing"),
        (QuestionCategory.MACRO_REGIME, "not_an_indicator"),
        (QuestionCategory.PORTFOLIO_GAP, None),
    ],
    indirect=["question__category", "question__signal_source"],
)
def test__fetch_deterministic_with_no_fetchable_source_returns_no_data(
    question: Question,
    deterministic_tools: DeterministicResearchTools,
) -> None:
    assert _fetch_deterministic(question, deterministic_tools) == NoData()


_FETCH_ERROR_REASON = "upstream 503; series temporarily unavailable"
_NO_DATA_ANSWER = "Data unavailable."


@pytest.mark.parametrize(
    ("question__category", "expected"),
    [
        (QuestionCategory.MACRO_REGIME, DataSourceToken.FRED_MCP),
        (QuestionCategory.PORTFOLIO_GAP, DataSourceToken.YFINANCE_MCP),
    ],
    indirect=["question__category"],
)
def test__deterministic_answer_with_fetch_value_uses_source_token(
    question: Question, expected: DataSourceToken
) -> None:
    result = _deterministic_answer(question, FetchValue(value=1.23))
    assert result.sources_used == [expected]


@pytest.mark.parametrize(
    ("question__category", "expected"),
    [
        (QuestionCategory.MACRO_REGIME, Confidence.HIGH),
        (QuestionCategory.PORTFOLIO_GAP, Confidence.MEDIUM),
    ],
    indirect=["question__category"],
)
def test__deterministic_answer_with_fetch_value_confidence(question: Question, expected: Confidence) -> None:
    result = _deterministic_answer(question, FetchValue(value=1.23))
    assert result.confidence == expected


def test__deterministic_answer_with_fetch_value_records_data(question: Question) -> None:
    result = _deterministic_answer(question, FetchValue(value=1.23))
    assert result.data_retrieved == {"value": 1.23}


def test__deterministic_answer_with_no_data_answer(question: Question) -> None:
    result = _deterministic_answer(question, NoData())
    assert result.answer == _NO_DATA_ANSWER


def test__deterministic_answer_with_no_data_empty_sources(question: Question) -> None:
    result = _deterministic_answer(question, NoData())
    assert result.sources_used == []


def test__deterministic_answer_with_fetch_error_answer(question: Question) -> None:
    result = _deterministic_answer(question, FetchError(reason=_FETCH_ERROR_REASON))
    assert result.answer == f"Data fetch error: {_FETCH_ERROR_REASON}"


def test__deterministic_answer_with_fetch_error_empty_sources(question: Question) -> None:
    result = _deterministic_answer(question, FetchError(reason=_FETCH_ERROR_REASON))
    assert result.sources_used == []


def test__deterministic_answer_with_fetch_error_has_no_data_retrieved(question: Question) -> None:
    result = _deterministic_answer(question, FetchError(reason=_FETCH_ERROR_REASON))
    assert result.data_retrieved is None


def test__deterministic_answer_with_fetch_error_logs_error(
    question: Question, loguru_records: list[CapturedLog]
) -> None:
    _ = _deterministic_answer(question, FetchError(reason=_FETCH_ERROR_REASON))
    assert any(
        record.level == "ERROR" and question.id in record.message and _FETCH_ERROR_REASON in record.message
        for record in loguru_records
    )


_BOGUS_QUESTION_ID = "Q999-unmatched"

_MATCHED_DRAFT = AnswerDraft(
    question_id="Q010",
    answer="The growth thesis still holds on current data.",
    sources_used=[DataSourceToken.BRAVE_SEARCH_MCP],
    data_retrieved=None,
    limitations="none",
)

_BOGUS_DRAFT = AnswerDraft(
    question_id=_BOGUS_QUESTION_ID,
    answer="Orphan draft with no originating question.",
    sources_used=[],
    data_retrieved=None,
    limitations="none",
)


@pytest.fixture
def source_ref() -> SourceRef:
    return SourceRef(
        source_id="src-1",
        source_type=SourceType.NEWSLETTER,
        title="Weekly Macro",
        url=None,
        published_at=None,
        retrieved_at="2026-07-01T00:00:00Z",
        locator=None,
    )


@pytest.fixture
def retrieval_open_ended_question(request: FixtureRequest) -> Question:
    return getattr(
        request,
        "param",
        Question(
            id="Q010",
            category=QuestionCategory.THESIS_VALIDATION,
            question="Does the growth thesis still hold?",
            signal_source="claim:c1",
            signal_tier="high",
            rationale="Thesis validation needs a fresh read on the underlying claim.",
            data_sources=[DataSourceToken.BRAVE_SEARCH_MCP],
            answer=None,
        ),
    )


@pytest.fixture
def retrieval_working_dir__questions(
    request: FixtureRequest,
    retrieval_open_ended_question: Question,
) -> list[Question]:
    return getattr(request, "param", [retrieval_open_ended_question])


@pytest.fixture
def retrieval_working_dir__sources(request: FixtureRequest, source_ref: SourceRef) -> list[SourceRef]:
    return getattr(request, "param", [source_ref])


@pytest.fixture
def retrieval_working_dir(
    tmp_path: Path,
    retrieval_working_dir__questions: list[Question],
    retrieval_working_dir__sources: list[SourceRef],
) -> Path:
    initial_questions = InitialQuestions(
        slug="test-run",
        generated_at="2026-07-01T00:00:00Z",
        signal_summary=SignalSummary(
            high_signal_count=0,
            medium_signal_count=0,
            low_signal_count=0,
            questions_generated=len(retrieval_working_dir__questions),
        ),
        questions=retrieval_working_dir__questions,
        error=None,
    )
    aggregated_signals = AggregatedSignals(
        slug="test-run",
        sources=retrieval_working_dir__sources,
        claims=[],
        corroborations=[],
        conflicts=[],
        has_actionable_content=False,
    )
    _ = (tmp_path / "initial_questions.json").write_text(initial_questions.model_dump_json(indent=2), encoding="utf-8")
    _ = (tmp_path / "aggregated_signals.json").write_text(
        aggregated_signals.model_dump_json(indent=2), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def answer_synthesis_agent__drafts(request: FixtureRequest) -> list[AnswerDraft]:
    return getattr(request, "param", [_MATCHED_DRAFT])


@pytest.fixture
def answer_synthesis_agent(
    answer_synthesis_agent__drafts: list[AnswerDraft],
) -> Callable[[list[Question], list[SourceRef]], list[AnswerDraft]]:
    def _agent(questions: list[Question], sources: list[SourceRef]) -> list[AnswerDraft]:
        return answer_synthesis_agent__drafts

    return _agent


@pytest.mark.parametrize(
    "answer_synthesis_agent__drafts",
    [[_MATCHED_DRAFT, _BOGUS_DRAFT]],
    indirect=True,
)
def test_make_retrieval_node_with_unmatched_draft_dropped(
    answer_synthesis_agent: Callable[[list[Question], list[SourceRef]], list[AnswerDraft]],
    deterministic_tools: DeterministicResearchTools,
    retrieval_working_dir: Path,
) -> None:
    node = make_retrieval_node(answer_synthesis_agent, deterministic_tools)
    _ = node({"working_dir": str(retrieval_working_dir), "slug": "test-run"})
    written = InitialAnswers.model_validate_json(
        (retrieval_working_dir / "initial_answers.json").read_text(encoding="utf-8")
    )
    assert _BOGUS_QUESTION_ID not in {answer.question_id for answer in written.answers}


@pytest.mark.parametrize(
    "answer_synthesis_agent__drafts",
    [[_MATCHED_DRAFT, _BOGUS_DRAFT]],
    indirect=True,
)
def test_make_retrieval_node_with_unmatched_draft_warns(
    answer_synthesis_agent: Callable[[list[Question], list[SourceRef]], list[AnswerDraft]],
    deterministic_tools: DeterministicResearchTools,
    retrieval_working_dir: Path,
    loguru_warnings: list[str],
) -> None:
    node = make_retrieval_node(answer_synthesis_agent, deterministic_tools)
    _ = node({"working_dir": str(retrieval_working_dir), "slug": "test-run"})
    assert any(_BOGUS_QUESTION_ID in message for message in loguru_warnings)
