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

Also pins the current-events evidence window: _evidence_cutoff_text backdates the source
published_at by the configured lookback, and fails soft — a missing or unparseable
published_at still emits the question rather than dropping a capital-relevant one.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pytest import FixtureRequest

from money_pit.compute.routing import CATEGORY_TO_TOOLS
from money_pit.pipeline.questions import _A2_AGENT_FAILURE_LOG
from money_pit.pipeline.questions import _MISSING_PUBLISHED_AT_TEXT
from money_pit.pipeline.questions import _UNPARSEABLE_PUBLISHED_AT_LOG
from money_pit.pipeline.questions import _draft_to_question
from money_pit.pipeline.questions import _evidence_cutoff_text
from money_pit.pipeline.questions import _make_current_events_questions
from money_pit.pipeline.questions import make_questions_node
from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import SourceType
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.questions import InitialQuestions
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.signals import Claim
from tests.unit_tests.conftest import CapturedLog


if TYPE_CHECKING:
    from money_pit.graph.state import PipelineState


_SLUG = "test-run"
_KNOWN_CLAIM_ID = "claim_high_001"
_UNKNOWN_CLAIM_ID = "claim_unknown_999"

_LOOKBACK_DAYS = 3
_PUBLISHED_AT = "2026-07-24T00:00:00Z"
_NAIVE_DATE_ONLY_PUBLISHED_AT = "2026-07-24"
_UNPARSEABLE_PUBLISHED_AT = "not-a-date"

_CLAIM_PUBLISHED_AT = "2026-01-15T12:00:00Z"
_CLAIM_CUTOFF_AT_LOOKBACK_DAYS = "2026-01-12T12:00:00Z"


def _source_ref() -> SourceRef:
    return SourceRef(
        source_id="yt_test_001",
        source_type=SourceType.NARRATED_VIDEO,
        title="Test Video: NVDA Thesis Review",
        url="https://www.youtube.com/watch?v=test",
        published_at=_CLAIM_PUBLISHED_AT,
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


def _claim_published_at(published_at: str | None) -> Claim:
    claim: Claim = _high_claim()
    return claim.model_copy(update={"source_ref": claim.source_ref.model_copy(update={"published_at": published_at})})


@pytest.mark.parametrize(
    ("lookback_days", "expected_cutoff"),
    [
        (0, "2026-07-24T00:00:00Z"),
        (1, "2026-07-23T00:00:00Z"),
        (7, "2026-07-17T00:00:00Z"),
        (30, "2026-06-24T00:00:00Z"),
    ],
)
def test__evidence_cutoff_text_with_lookback_days(lookback_days: int, expected_cutoff: str) -> None:
    assert _evidence_cutoff_text(_PUBLISHED_AT, lookback_days) == expected_cutoff


@pytest.mark.parametrize(
    ("published_at", "expected_cutoff"),
    [
        ("2026-07-24T00:00:00+00:00", "2026-07-21T00:00:00Z"),
        ("2026-07-24T00:00:00-05:00", "2026-07-21T05:00:00Z"),
    ],
)
def test__evidence_cutoff_text_with_offset_bearing_published_at(published_at: str, expected_cutoff: str) -> None:
    assert _evidence_cutoff_text(published_at, _LOOKBACK_DAYS) == expected_cutoff


@pytest.mark.parametrize(
    ("published_at", "expected_cutoff"),
    [
        (_NAIVE_DATE_ONLY_PUBLISHED_AT, "2026-07-21T00:00:00Z"),
        ("2026-07-24T06:30:00", "2026-07-21T06:30:00Z"),
    ],
)
def test__evidence_cutoff_text_with_naive_published_at_is_read_as_utc(published_at: str, expected_cutoff: str) -> None:
    assert _evidence_cutoff_text(published_at, _LOOKBACK_DAYS) == expected_cutoff


def test__evidence_cutoff_text_with_missing_published_at() -> None:
    assert _evidence_cutoff_text(None, _LOOKBACK_DAYS) == _MISSING_PUBLISHED_AT_TEXT


def test__evidence_cutoff_text_with_unparseable_published_at() -> None:
    assert _evidence_cutoff_text(_UNPARSEABLE_PUBLISHED_AT, _LOOKBACK_DAYS) == _MISSING_PUBLISHED_AT_TEXT


def test__make_current_events_questions_with_published_at_backdates_question_text() -> None:
    questions = _make_current_events_questions([_claim_published_at(_PUBLISHED_AT)], _LOOKBACK_DAYS)

    assert len(questions) == 1
    assert "since 2026-07-21T00:00:00Z" in questions[0].question


def test__make_current_events_questions_with_naive_published_at_backdates_question_text() -> None:
    questions = _make_current_events_questions([_claim_published_at(_NAIVE_DATE_ONLY_PUBLISHED_AT)], _LOOKBACK_DAYS)

    assert len(questions) == 1
    assert "since 2026-07-21T00:00:00Z" in questions[0].question


def test__make_current_events_questions_with_missing_published_at() -> None:
    questions = _make_current_events_questions([_claim_published_at(None)], _LOOKBACK_DAYS)

    assert len(questions) == 1
    assert f"since {_MISSING_PUBLISHED_AT_TEXT}" in questions[0].question


def test__make_current_events_questions_with_unparseable_published_at_fails_soft(
    loguru_records: list[CapturedLog],
) -> None:
    questions = _make_current_events_questions([_claim_published_at(_UNPARSEABLE_PUBLISHED_AT)], _LOOKBACK_DAYS)

    assert any(
        record.level == "WARNING"
        and record.message == _UNPARSEABLE_PUBLISHED_AT_LOG.format(published_at=_UNPARSEABLE_PUBLISHED_AT)
        for record in loguru_records
    )
    assert len(questions) == 1
    assert f"since {_MISSING_PUBLISHED_AT_TEXT}?" in questions[0].question


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
    node = make_questions_node(stub_claim_questions_agent, current_events_lookback_days=_LOOKBACK_DAYS)
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


def test_make_questions_node_forwards_current_events_lookback_days_into_question_text(
    initial_questions: InitialQuestions,
) -> None:
    current_events_qs = [q for q in initial_questions.questions if q.category == QuestionCategory.CURRENT_EVENTS]

    assert len(current_events_qs) == 1
    assert f"since {_CLAIM_CUTOFF_AT_LOOKBACK_DAYS}?" in current_events_qs[0].question


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


def test_make_questions_node_with_failing_agent_logs_failure_loudly(
    questions_working_dir: Path,
    raising_claim_questions_agent: Callable[[list[Claim]], list[DraftQuestion]],
    loguru_records: list[CapturedLog],
) -> None:
    node = make_questions_node(raising_claim_questions_agent, current_events_lookback_days=_LOOKBACK_DAYS)

    _ = node({"slug": _SLUG, "working_dir": str(questions_working_dir)})

    assert any(record.level == "ERROR" and record.message == _A2_AGENT_FAILURE_LOG for record in loguru_records)
