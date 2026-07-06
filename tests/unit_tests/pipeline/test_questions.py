"""Tests for money_pit.pipeline.questions — A2 draft→node boundary (wave S7, theme T1).

Pins the draft→node→contract pattern for A2:
- claim_questions_agent authors only list[DraftQuestion] ({category, question,
  signal_source, rationale});
- the pure helper _draft_to_question derives the deterministic fields —
  data_sources from the routing table, signal_tier from the originating claim,
  answer=None, id="" — and drops (returns None) any draft whose signal_source is
  not a known high/medium claim_id;
- make_questions_node runs each DraftQuestion through the helper (dropping None)
  before merging with the templated questions and writing initial_questions.json.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from pytest import FixtureRequest

from money_pit.compute.routing import CATEGORY_TO_TOOLS
from money_pit.graph.state import PipelineState
from money_pit.pipeline.questions import _draft_to_question, make_questions_node
from money_pit.schemas.enums import (
    ClaimCategory,
    QuestionCategory,
    SignalTier,
    SourceType,
)
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.questions import InitialQuestions
from money_pit.schemas.signals import AggregatedSignals, Claim

_SLUG = "test-run"
_KNOWN_CLAIM_ID = "claim_high_001"
_UNKNOWN_CLAIM_ID = "claim_unknown_999"


def _source_ref() -> SourceRef:
    return SourceRef(
        source_id="yt_test_001",
        source_type=SourceType.NARRATED_VIDEO,
        title="Test Video: NVDA Thesis Review",
        url="https://www.youtube.com/watch?v=test",
        published_at="2026-01-15T12:00:00Z",
        retrieved_at="2026-06-18T14:30:00Z",
        locator=None,
    )


def _high_claim() -> Claim:
    return Claim(
        claim_id=_KNOWN_CLAIM_ID,
        tier=SignalTier.HIGH,
        claim="NVDA data center revenue rose 112% year-over-year.",
        category=ClaimCategory.FUNDAMENTAL,
        tickers_affected=["NVDA"],
        requires_validation=True,
        source_ref=_source_ref(),
        cited_sources=[],
    )


def _known_draft() -> DraftQuestion:
    return DraftQuestion(
        category=QuestionCategory.THESIS_VALIDATION,
        question="Does NVDA's latest 10-Q confirm the 112% data-center growth thesis?",
        signal_source=_KNOWN_CLAIM_ID,
        rationale="Direct check of the load-bearing fundamental claim.",
    )


def _unknown_draft() -> DraftQuestion:
    return DraftQuestion(
        category=QuestionCategory.THESIS_VALIDATION,
        question="Does the phantom claim still hold?",
        signal_source=_UNKNOWN_CLAIM_ID,
        rationale="References a claim absent from the high/medium set.",
    )


def test__draft_to_question_with_known_claim_sets_data_sources_from_category() -> None:
    result = _draft_to_question(_known_draft(), {_KNOWN_CLAIM_ID: _high_claim()})

    assert result is not None
    assert result.data_sources == CATEGORY_TO_TOOLS[QuestionCategory.THESIS_VALIDATION]


def test__draft_to_question_with_known_claim_sets_signal_tier_from_claim() -> None:
    result = _draft_to_question(_known_draft(), {_KNOWN_CLAIM_ID: _high_claim()})

    assert result is not None
    assert result.signal_tier == SignalTier.HIGH


def test__draft_to_question_with_known_claim_sets_answer_none() -> None:
    result = _draft_to_question(_known_draft(), {_KNOWN_CLAIM_ID: _high_claim()})

    assert result is not None
    assert result.answer is None


def test__draft_to_question_with_unknown_claim_returns_none() -> None:
    result = _draft_to_question(_unknown_draft(), {_KNOWN_CLAIM_ID: _high_claim()})

    assert result is None


@pytest.fixture
def questions_working_dir__claims(request: FixtureRequest) -> list[Claim]:
    return getattr(request, "param", [_high_claim()])


@pytest.fixture
def questions_working_dir(tmp_path: Path, questions_working_dir__claims: list[Claim]) -> Path:
    aggregated_signals = AggregatedSignals(
        slug=_SLUG,
        sources=[],
        claims=questions_working_dir__claims,
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
    _ = (tmp_path / "aggregated_signals.json").write_text(
        aggregated_signals.model_dump_json(indent=2), encoding="utf-8"
    )
    _ = (tmp_path / "portfolio_snapshot.json").write_text(
        portfolio_snapshot.model_dump_json(indent=2), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def stub_claim_questions_agent__drafts(request: FixtureRequest) -> list[DraftQuestion]:
    return getattr(request, "param", [_known_draft(), _unknown_draft()])


@pytest.fixture
def stub_claim_questions_agent(
    stub_claim_questions_agent__drafts: list[DraftQuestion],
) -> Callable[[list[Claim]], list[DraftQuestion]]:
    def _agent(_claims: list[Claim]) -> list[DraftQuestion]:
        return stub_claim_questions_agent__drafts

    return _agent


@pytest.fixture
def initial_questions(
    questions_working_dir: Path,
    stub_claim_questions_agent: Callable[[list[Claim]], list[DraftQuestion]],
) -> InitialQuestions:
    node = make_questions_node(stub_claim_questions_agent)
    state: PipelineState = {"slug": _SLUG, "working_dir": str(questions_working_dir)}
    _ = node(state)
    return InitialQuestions.model_validate_json(
        (questions_working_dir / "initial_questions.json").read_text(encoding="utf-8")
    )


def test_make_questions_node_converts_known_claim_draft_data_sources(
    initial_questions: InitialQuestions,
) -> None:
    thesis_qs = [q for q in initial_questions.questions if q.category == QuestionCategory.THESIS_VALIDATION]

    assert len(thesis_qs) == 1
    assert thesis_qs[0].data_sources == CATEGORY_TO_TOOLS[QuestionCategory.THESIS_VALIDATION]


def test_make_questions_node_converts_known_claim_draft_signal_tier(
    initial_questions: InitialQuestions,
) -> None:
    thesis_qs = [q for q in initial_questions.questions if q.category == QuestionCategory.THESIS_VALIDATION]

    assert len(thesis_qs) == 1
    assert thesis_qs[0].signal_tier == SignalTier.HIGH


def test_make_questions_node_drops_unknown_claim_draft(
    initial_questions: InitialQuestions,
) -> None:
    sources = [q.signal_source for q in initial_questions.questions]

    assert _UNKNOWN_CLAIM_ID not in sources


@pytest.fixture
def raising_claim_questions_agent() -> Callable[[list[Claim]], list[DraftQuestion]]:
    def _agent(_claims: list[Claim]) -> list[DraftQuestion]:
        raise RuntimeError("claim_questions_agent boom")

    return _agent


def test_make_questions_node_with_failing_agent_warns(
    questions_working_dir: Path,
    raising_claim_questions_agent: Callable[[list[Claim]], list[DraftQuestion]],
    loguru_warnings: list[str],
) -> None:
    node = make_questions_node(raising_claim_questions_agent)

    _ = node({"slug": _SLUG, "working_dir": str(questions_working_dir)})

    assert loguru_warnings
