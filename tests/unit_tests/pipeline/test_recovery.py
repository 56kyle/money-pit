"""Tests for money_pit.pipeline.recovery."""

from pathlib import Path

import pytest

from money_pit.alpaca_orders import FillObservationError
from money_pit.alpaca_orders import OrderNotYetVisibleError
from money_pit.compute.fills import build_fill_observation
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.pipeline.recovery import _NOT_FOUND_STATUS
from money_pit.pipeline.recovery import _UNOBSERVABLE_STATUS
from money_pit.pipeline.recovery import _find_prior_journal
from money_pit.pipeline.recovery import reconcile_prior_run
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.enums import RecoveryDecision
from money_pit.schemas.fills import FillObservation
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.journal import ExecutionJournalEntry


CURRENT_SLUG = "2026-07-13"
PRIOR_SLUG = "2026-07-12"


def _make_entry(slug: str, step_id: str, phase: ExecutionPhase) -> ExecutionJournalEntry:
    return ExecutionJournalEntry(
        step_id=step_id,
        group_id=None,
        client_order_id=f"{slug}:{step_id}",
        phase=phase,
        intended={},
        broker_order_id=f"broker-{step_id}",
        status=None,
        filled_qty=None,
        filled_avg_price=None,
        realized_notional=None,
        compensation_of=None,
        error=None,
        timestamp="2026-07-12T00:00:00Z",
    )


def _write_journal(
    daily_show_root: Path,
    slug: str,
    outcome: ExecutionOutcome | None,
    entries: list[ExecutionJournalEntry],
) -> Path:
    run_dir = daily_show_root / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    journal = ExecutionJournal(slug=slug, outcome=outcome, entries=entries)
    journal_path = run_dir / EXECUTION_JOURNAL_FILENAME
    journal_path.write_text(journal.model_dump_json(), encoding="utf-8")
    return journal_path


class _ScriptedObserver:
    """A real FillObserver: returns a scripted observation per client_order_id, or raises the scripted error."""

    def __init__(self, responses: dict[str, FillObservation | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, client_order_id: str) -> FillObservation:
        self.calls.append(client_order_id)
        response = self.responses[client_order_id]
        if isinstance(response, Exception):
            raise response
        return response


def test_reconcile_prior_run_with_empty_root(tmp_path: Path) -> None:
    observer = _ScriptedObserver({})

    result = reconcile_prior_run(tmp_path, CURRENT_SLUG, observer)

    assert result.decision is RecoveryDecision.PROCEED
    assert result.prior_slug is None
    assert result.prior_outcome is None
    assert result.open_orders == []
    assert result.settled_orders == []
    assert observer.calls == []


def test_reconcile_prior_run_with_prior_dir_but_no_journal(tmp_path: Path) -> None:
    (tmp_path / PRIOR_SLUG).mkdir()
    observer = _ScriptedObserver({})

    result = reconcile_prior_run(tmp_path, CURRENT_SLUG, observer)

    assert result.decision is RecoveryDecision.PROCEED
    assert result.prior_slug is None
    assert result.open_orders == []
    assert result.settled_orders == []
    assert observer.calls == []


def test_reconcile_prior_run_with_executed_clean(tmp_path: Path) -> None:
    _write_journal(
        tmp_path,
        PRIOR_SLUG,
        ExecutionOutcome.EXECUTED_CLEAN,
        [_make_entry(PRIOR_SLUG, "s1", ExecutionPhase.FILLED)],
    )
    observer = _ScriptedObserver({})

    result = reconcile_prior_run(tmp_path, CURRENT_SLUG, observer)

    assert result.decision is RecoveryDecision.PROCEED
    assert result.prior_slug == PRIOR_SLUG
    assert result.prior_outcome is ExecutionOutcome.EXECUTED_CLEAN
    assert result.open_orders == []
    assert result.settled_orders == []
    assert observer.calls == []


def test_reconcile_prior_run_with_execution_failed(tmp_path: Path) -> None:
    _write_journal(
        tmp_path,
        PRIOR_SLUG,
        ExecutionOutcome.EXECUTION_FAILED,
        [_make_entry(PRIOR_SLUG, "s1", ExecutionPhase.REJECTED)],
    )
    observer = _ScriptedObserver({})

    result = reconcile_prior_run(tmp_path, CURRENT_SLUG, observer)

    assert result.decision is RecoveryDecision.PROCEED
    assert result.open_orders == []
    assert result.settled_orders == []
    assert observer.calls == []


@pytest.mark.parametrize("prior_outcome", [ExecutionOutcome.EXECUTED_INCOMPLETE, None])
@pytest.mark.parametrize("open_phase", [ExecutionPhase.SUBMITTED, ExecutionPhase.PARTIALLY_FILLED])
def test_reconcile_prior_run_with_open_leg_now_settled(
    tmp_path: Path,
    prior_outcome: ExecutionOutcome | None,
    open_phase: ExecutionPhase,
) -> None:
    entry = _make_entry(PRIOR_SLUG, "s1", open_phase)
    _write_journal(tmp_path, PRIOR_SLUG, prior_outcome, [entry])
    observer = _ScriptedObserver(
        {entry.client_order_id: build_fill_observation("filled", 8.0, 100.0)}
    )

    result = reconcile_prior_run(tmp_path, CURRENT_SLUG, observer)

    assert result.decision is RecoveryDecision.PROCEED_WITH_NOTICE
    assert observer.calls == [entry.client_order_id]
    assert result.open_orders == []
    assert len(result.settled_orders) == 1
    settled = result.settled_orders[0]
    assert settled.step_id == "s1"
    assert settled.client_order_id == entry.client_order_id
    assert settled.observed_status == "filled"
    assert settled.phase is ExecutionPhase.FILLED


@pytest.mark.parametrize("prior_outcome", [ExecutionOutcome.EXECUTED_INCOMPLETE, None])
def test_reconcile_prior_run_with_open_leg_still_open(
    tmp_path: Path,
    prior_outcome: ExecutionOutcome | None,
) -> None:
    entry = _make_entry(PRIOR_SLUG, "s1", ExecutionPhase.SUBMITTED)
    _write_journal(tmp_path, PRIOR_SLUG, prior_outcome, [entry])
    observer = _ScriptedObserver(
        {entry.client_order_id: build_fill_observation("accepted", None, None)}
    )

    result = reconcile_prior_run(tmp_path, CURRENT_SLUG, observer)

    assert result.decision is RecoveryDecision.HALT
    assert result.settled_orders == []
    assert len(result.open_orders) == 1
    still_open = result.open_orders[0]
    assert still_open.client_order_id == entry.client_order_id
    assert still_open.observed_status == "accepted"
    assert still_open.phase is ExecutionPhase.SUBMITTED


def test_reconcile_prior_run_with_partially_filled_entry_still_open(tmp_path: Path) -> None:
    entry = _make_entry(PRIOR_SLUG, "s1", ExecutionPhase.PARTIALLY_FILLED)
    _write_journal(tmp_path, PRIOR_SLUG, ExecutionOutcome.EXECUTED_INCOMPLETE, [entry])
    observer = _ScriptedObserver(
        {entry.client_order_id: build_fill_observation("partially_filled", 3.0, 100.0)}
    )

    result = reconcile_prior_run(tmp_path, CURRENT_SLUG, observer)

    assert result.decision is RecoveryDecision.HALT
    assert result.settled_orders == []
    assert len(result.open_orders) == 1
    assert result.open_orders[0].phase is ExecutionPhase.PARTIALLY_FILLED
    assert result.open_orders[0].observed_status == "partially_filled"


def test_reconcile_prior_run_with_order_not_yet_visible(tmp_path: Path) -> None:
    entry = _make_entry(PRIOR_SLUG, "s1", ExecutionPhase.SUBMITTED)
    _write_journal(tmp_path, PRIOR_SLUG, ExecutionOutcome.EXECUTED_INCOMPLETE, [entry])
    observer = _ScriptedObserver({entry.client_order_id: OrderNotYetVisibleError()})

    result = reconcile_prior_run(tmp_path, CURRENT_SLUG, observer)

    assert result.decision is RecoveryDecision.PROCEED_WITH_NOTICE
    assert result.open_orders == []
    assert len(result.settled_orders) == 1
    settled = result.settled_orders[0]
    assert settled.observed_status == _NOT_FOUND_STATUS
    assert settled.phase is ExecutionPhase.SUBMITTED


def test_reconcile_prior_run_with_fill_observation_error(tmp_path: Path) -> None:
    entry = _make_entry(PRIOR_SLUG, "s1", ExecutionPhase.SUBMITTED)
    _write_journal(tmp_path, PRIOR_SLUG, ExecutionOutcome.EXECUTED_INCOMPLETE, [entry])
    observer = _ScriptedObserver({entry.client_order_id: FillObservationError()})

    result = reconcile_prior_run(tmp_path, CURRENT_SLUG, observer)

    assert result.decision is RecoveryDecision.HALT
    assert result.settled_orders == []
    assert len(result.open_orders) == 1
    unobservable = result.open_orders[0]
    assert unobservable.observed_status == _UNOBSERVABLE_STATUS
    assert unobservable.phase is ExecutionPhase.SUBMITTED


@pytest.mark.parametrize(
    ("run_specs", "expected_prior_slug"),
    [
        ([("2026-07-10", True), ("2026-07-11", True), ("2026-07-12", True)], "2026-07-12"),
        ([("2026-07-12", True), ("2026-07-14", True)], "2026-07-12"),
        ([("2026-07-12", True), ("2026-07-13", True)], "2026-07-12"),
        ([("2026-07-11", True), ("2026-07-12", False)], "2026-07-11"),
        ([("2026-07-13", True), ("2026-07-14", True)], None),
        ([], None),
    ],
)
def test__find_prior_journal(
    tmp_path: Path,
    run_specs: list[tuple[str, bool]],
    expected_prior_slug: str | None,
) -> None:
    for slug, has_journal in run_specs:
        if has_journal:
            _write_journal(tmp_path, slug, ExecutionOutcome.EXECUTED_CLEAN, [])
        else:
            (tmp_path / slug).mkdir()

    result = _find_prior_journal(tmp_path, CURRENT_SLUG)

    if expected_prior_slug is None:
        assert result is None
    else:
        assert result == tmp_path / expected_prior_slug / EXECUTION_JOURNAL_FILENAME
