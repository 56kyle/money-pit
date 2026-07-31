"""Tests for LLM factory closures and their deterministic message boundaries."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Generic
from typing import TypeVar

import pytest
from pytest import MonkeyPatch

from money_pit.adapters import text_llm
from money_pit.adapters import video_llm
from money_pit.adapters.text_llm import TextPayload
from money_pit.adapters.video_llm import TranscriptSource
from money_pit.adapters.video_llm import VideoPayload
from money_pit.agents import answer_synthesis
from money_pit.agents import claim_questions
from money_pit.agents import thesis_judgment
from money_pit.config import Config
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.answer_draft import AnswerDraft
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import DataSourceToken
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import SourceType
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.questions import Question
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.signals import Claim


_OutputT = TypeVar("_OutputT")


@dataclass
class _RunCall:
    message: str
    deps: object | None


class _FakeAgent(Generic[_OutputT]):
    def __init__(self, output: _OutputT) -> None:
        self.output = output
        self.calls: list[_RunCall] = []
        self.tools: list[object] = []

    def tool(self, function: object) -> object:
        self.tools.append(function)
        return function

    def run_sync(self, message: str, *, deps: object | None = None) -> object:
        self.calls.append(_RunCall(message=message, deps=deps))
        return SimpleNamespace(output=self.output)


@pytest.fixture
def config() -> Config:
    return Config(
        alpaca_service="alpaca",
        alpaca_username="key",
        alpaca_paper=True,
        llm_model="default-model",
    )


@pytest.fixture
def source_ref() -> SourceRef:
    return SourceRef(
        source_id="source-1",
        source_type=SourceType.MANUAL_NOTE,
        title="Source",
        url=None,
        published_at=None,
        retrieved_at="2026-07-29T14:00:00Z",
        locator=None,
    )


@pytest.fixture
def signal_draft(source_ref: SourceRef) -> SignalSetDraft:
    return SignalSetDraft(
        source_id=source_ref.source_id,
        source_type=source_ref.source_type.value,
        title=source_ref.title,
        url=source_ref.url,
        published_at=source_ref.published_at,
        retrieved_at=source_ref.retrieved_at,
        summary="Summary",
        claims=[],
        tickers_mentioned=[],
        sectors_mentioned=[],
        macro_themes=[],
    )


def test_make_text_llm_agent_runs_payload_and_returns_output(
    monkeypatch: MonkeyPatch,
    config: Config,
    source_ref: SourceRef,
    signal_draft: SignalSetDraft,
) -> None:
    agent = _FakeAgent(signal_draft)
    monkeypatch.setattr(text_llm, "Agent", lambda *_args, **_kwargs: agent)
    payload = TextPayload(slug="run-1", source_ref=source_ref, body="Thesis")

    result = text_llm.make_text_llm_agent(config, model="override")(payload)

    assert result is signal_draft
    assert "## Thesis\nThesis" in agent.calls[0].message


def test_make_video_llm_agent_runs_payload_and_returns_output(
    monkeypatch: MonkeyPatch,
    config: Config,
    source_ref: SourceRef,
    signal_draft: SignalSetDraft,
) -> None:
    agent = _FakeAgent(signal_draft)
    monkeypatch.setattr(video_llm, "Agent", lambda *_args, **_kwargs: agent)
    payload = VideoPayload(
        slug="run-1",
        source_ref=source_ref,
        transcript="Transcript",
        transcript_source=TranscriptSource.WHISPER,
        has_word_timestamps=True,
        on_screen_text=[],
    )

    result = video_llm.make_video_llm_agent(config, model="override")(payload)

    assert result is signal_draft
    assert "## Transcript\nTranscript" in agent.calls[0].message


def _question(category: QuestionCategory) -> Question:
    return Question(
        id="q-1",
        category=category,
        question="What changed?",
        signal_source="claim-1",
        signal_tier=SignalTier.HIGH,
        rationale="Material.",
        data_sources=[DataSourceToken.BRAVE_SEARCH_MCP],
        answer=None,
    )


class _OpenEndedTools:
    def brave_search(self, query: str, *, n_results: int = 5) -> list[str]:
        return [f"{query}:{n_results}"]

    def edgar_search(self, query: str, *, n_results: int = 5) -> list[str]:
        _ = (query, n_results)
        return []


def test_make_answer_synthesis_agent_filters_and_runs_open_ended_questions(
    monkeypatch: MonkeyPatch,
    config: Config,
    source_ref: SourceRef,
) -> None:
    output = [
        AnswerDraft(
            question_id="q-1",
            answer="Answer",
            sources_used=[DataSourceToken.BRAVE_SEARCH_MCP],
            data_retrieved=None,
            limitations="None",
        )
    ]
    agent = _FakeAgent(output)
    monkeypatch.setattr(answer_synthesis, "Agent", lambda *_args, **_kwargs: agent)
    tools = _OpenEndedTools()
    run = answer_synthesis.make_answer_synthesis_agent(tools, config)

    result = run(
        [_question(QuestionCategory.MACRO_REGIME), _question(QuestionCategory.CURRENT_EVENTS)],
        [source_ref],
    )

    assert result == output
    assert "current_events" in agent.calls[0].message
    assert "macro_regime" not in agent.calls[0].message
    assert agent.calls[0].deps is tools


def test_make_answer_synthesis_agent_with_no_open_ended_questions_skips_model(
    monkeypatch: MonkeyPatch,
    config: Config,
) -> None:
    agent: _FakeAgent[list[AnswerDraft]] = _FakeAgent([])
    monkeypatch.setattr(answer_synthesis, "Agent", lambda *_args, **_kwargs: agent)
    run = answer_synthesis.make_answer_synthesis_agent(_OpenEndedTools(), config)

    result = run([_question(QuestionCategory.MACRO_REGIME)], [])

    assert result == []
    assert agent.calls == []


def test_make_answer_synthesis_agent_tools_render_results_and_empty(
    monkeypatch: MonkeyPatch,
    config: Config,
) -> None:
    agent: _FakeAgent[list[AnswerDraft]] = _FakeAgent([])
    monkeypatch.setattr(answer_synthesis, "Agent", lambda *_args, **_kwargs: agent)
    tools = _OpenEndedTools()
    _ = answer_synthesis.make_answer_synthesis_agent(tools, config)
    context = SimpleNamespace(deps=tools)

    brave_result = agent.tools[0](context, "query", 2)  # type: ignore[operator]
    edgar_result = agent.tools[1](context, "query", 2)  # type: ignore[operator]

    assert brave_result == "query:2"
    assert edgar_result == "No results found."


def test_make_claim_questions_agent_runs_claims(
    monkeypatch: MonkeyPatch,
    config: Config,
    source_ref: SourceRef,
) -> None:
    output = [
        DraftQuestion(
            category=QuestionCategory.THESIS_VALIDATION,
            question="Supported?",
            signal_source="claim-1",
            rationale="Material.",
        )
    ]
    agent = _FakeAgent(output)
    monkeypatch.setattr(claim_questions, "Agent", lambda *_args, **_kwargs: agent)
    claim = Claim(
        claim_id="claim-1",
        tier=SignalTier.HIGH,
        claim="Claim",
        category="fundamental",
        tickers_affected=["SPY"],
        requires_validation=True,
        source_ref=source_ref,
        cited_sources=[],
    )

    result = claim_questions.make_claim_questions_agent(config)([claim])

    assert result == output
    assert "claim-1" in agent.calls[0].message


def test_make_thesis_judgment_agent_runs_all_three_inputs(
    monkeypatch: MonkeyPatch,
    config: Config,
    source_ref: SourceRef,
) -> None:
    output = AnalysisJudgment(theses=[], dropped_claims=[], macro_read=[], halt=None)
    agent = _FakeAgent(output)
    monkeypatch.setattr(thesis_judgment, "Agent", lambda *_args, **_kwargs: agent)
    signals = AggregatedSignals(
        slug="run-1",
        sources=[source_ref],
        claims=[],
        corroborations=[],
        conflicts=[],
        has_actionable_content=False,
    )
    portfolio = PortfolioSnapshot(
        slug="run-1",
        as_of="2026-07-29T14:00:00Z",
        total_account_value=100.0,
        available_cash=100.0,
        positions=[],
        sector_weights={},
        correlated_overlaps=[],
    )
    answers = InitialAnswers(slug="run-1", sources=[source_ref], answers=[])

    result = thesis_judgment.make_thesis_judgment_agent(config)(signals, portfolio, answers)

    assert result is output
    assert all(
        header in agent.calls[0].message
        for header in ("## Aggregated Signals", "## Portfolio Snapshot", "## Research Answers")
    )
