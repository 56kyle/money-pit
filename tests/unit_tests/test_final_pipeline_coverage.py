"""Tests pinning the final reachable pipeline and storage branches."""
# pyright: reportPrivateUsage=false

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pytest import MonkeyPatch

from money_pit.compute.fills import build_fill_observation
from money_pit.config import Config
from money_pit.pipeline.execution import ExecutionAuthorityUnavailableError
from money_pit.pipeline.execution import make_execution_node
from money_pit.pipeline.questions import _make_portfolio_gap_questions
from money_pit.pipeline.recovery import _prior_slug_candidates
from money_pit.pipeline.retrieval import _answer_open_ended
from money_pit.pipeline.retrieval import _combine_fetch_results
from money_pit.pipeline.retrieval import _fetch_deterministic
from money_pit.pipeline.stages import _build_aggregator_node
from money_pit.schemas.answer_draft import AnswerDraft
from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import SourceType
from money_pit.schemas.fetch_result import FetchResult
from money_pit.schemas.fetch_result import FetchValue
from money_pit.schemas.fetch_result import NoData
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.portfolio import Position
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.questions import Question
from money_pit.schemas.signals import Claim
from money_pit.storage.database import Database
from money_pit.storage.errors import MigrationDiscoveryError
from money_pit.storage.errors import StorageConnectionError
from money_pit.storage.migrations import discover_migrations
from tests.unit_tests.pipeline.test_execution import _ACTION_STEPS_ADAPTER
from tests.unit_tests.pipeline.test_execution import _POLL_INTERVAL
from tests.unit_tests.pipeline.test_execution import _POLL_TIMEOUT
from tests.unit_tests.pipeline.test_execution import _SLUG
from tests.unit_tests.pipeline.test_execution import _make_action_step
from tests.unit_tests.pipeline.test_execution import _read_journal
from tests.unit_tests.pipeline.test_execution import _ScriptedObserver


if TYPE_CHECKING:
    from money_pit.agents.research_tools import DeterministicResearchTools


def test__build_aggregator_node(monkeypatch: MonkeyPatch) -> None:
    def expected_node(state):
        return state

    monkeypatch.setattr(
        "money_pit.pipeline.stages.corroboration_agent_or_default",
        lambda _overrides: object(),
    )

    def build_node(*, corroboration_agent: object):
        assert corroboration_agent is not None
        return expected_node

    monkeypatch.setattr(
        "money_pit.pipeline.stages.make_aggregator_node",
        build_node,
    )

    assert _build_aggregator_node(Config.model_construct()) is expected_node


def test__prior_slug_candidates_with_absent_root(tmp_path: Path) -> None:
    assert list(_prior_slug_candidates(tmp_path / "absent", "2026-07-13")) == []


def _question(category: QuestionCategory) -> Question:
    return Question(
        id="Q001",
        category=category,
        question="What changed?",
        signal_source=None,
        signal_tier=SignalTier.HIGH,
        rationale="Test the retrieval boundary.",
        data_sources=[],
        answer=None,
    )


class _UnusedTools:
    def fetch_fred_series(self, _series_id: str) -> FetchResult:
        raise AssertionError

    def fetch_ticker_price(self, _ticker: str) -> FetchResult:
        raise AssertionError


def test__combine_fetch_results_with_no_data_before_value() -> None:
    assert _combine_fetch_results([NoData(), FetchValue(value=6.0)]) == FetchValue(value=6.0)


def test__fetch_deterministic_with_open_ended_category() -> None:
    tools: DeterministicResearchTools = _UnusedTools()
    assert _fetch_deterministic(_question(QuestionCategory.CURRENT_EVENTS), tools) == NoData()


def test__answer_open_ended_with_agent_failure() -> None:
    def failing_agent(
        _questions: list[Question],
        _sources: list[SourceRef],
    ) -> list[AnswerDraft]:
        raise RuntimeError("agent unavailable")

    assert _answer_open_ended(failing_agent, [_question(QuestionCategory.CURRENT_EVENTS)], []) == []


def test_make_execution_node_without_order_authority_journals_intent(tmp_path: Path) -> None:
    steps = [_make_action_step("A001")]
    _ = (tmp_path / "action_steps.json").write_text(
        _ACTION_STEPS_ADAPTER.dump_json(steps, indent=2).decode("utf-8"),
        encoding="utf-8",
    )
    node = make_execution_node(
        None,
        _ScriptedObserver(build_fill_observation("filled", 1.0, 1.0)),
        poll_interval=_POLL_INTERVAL,
        poll_timeout=_POLL_TIMEOUT,
    )

    with pytest.raises(ExecutionAuthorityUnavailableError):
        _ = node({"slug": _SLUG, "working_dir": str(tmp_path)})

    assert _read_journal(tmp_path).entries[0].phase is ExecutionPhase.PLANNED


def _claim() -> Claim:
    source = SourceRef(
        source_id="source",
        source_type=SourceType.NARRATED_VIDEO,
        title="Source",
        url=None,
        published_at=None,
        retrieved_at="2026-07-01T00:00:00Z",
        locator=None,
    )
    return Claim(
        claim_id="claim",
        tier=SignalTier.HIGH,
        claim="NVDA revenue increased.",
        category=ClaimCategory.FUNDAMENTAL,
        tickers_affected=["NVDA"],
        requires_validation=True,
        source_ref=source,
        cited_sources=[],
    )


def _position(ticker: str) -> Position:
    return Position(
        ticker=ticker,
        quantity=1.0,
        cost_basis=100.0,
        current_value=100.0,
        unrealized_pl=0.0,
        sector="Technology",
        factor_tags=[],
    )


def _portfolio(*positions: Position) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        slug="run",
        as_of="2026-07-01T00:00:00Z",
        total_account_value=1000.0,
        available_cash=500.0,
        positions=list(positions),
        sector_weights={},
        correlated_overlaps=[],
    )


def test__make_portfolio_gap_questions_with_matching_and_nonmatching_positions() -> None:
    questions = _make_portfolio_gap_questions([_claim()], _portfolio(_position("MSFT"), _position("NVDA")))
    assert [question.signal_source for question in questions] == ["NVDA"]


def test__make_portfolio_gap_questions_with_empty_portfolio() -> None:
    questions = _make_portfolio_gap_questions([_claim()], _portfolio())
    assert questions[0].signal_source is None


@pytest.mark.parametrize("busy_timeout_ms", [0, -1])
def test_database_with_nonpositive_busy_timeout(tmp_path: Path, busy_timeout_ms: int) -> None:
    with pytest.raises(ValueError, match="busy_timeout_ms must be positive"):
        _ = Database(tmp_path / "state.sqlite3", busy_timeout_ms=busy_timeout_ms)


def test__connect_closes_connection_after_sqlite_setup_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    database = Database(tmp_path / "state.sqlite3")
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)

    def fail(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("setup")

    monkeypatch.setattr(database, "_require_capabilities", fail)
    with pytest.raises(StorageConnectionError):
        _ = database._connect()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_discover_migrations_with_noncontiguous_sequence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _ = (tmp_path / "0002_second.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr("money_pit.storage.migrations.importlib.resources.files", lambda _package: tmp_path)
    with pytest.raises(MigrationDiscoveryError):
        _ = discover_migrations()
