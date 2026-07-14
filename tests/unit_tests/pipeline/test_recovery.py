"""Tests for money_pit.pipeline.recovery."""

from pathlib import Path

import pytest

from money_pit.alpaca_orders import FillObservationError
from money_pit.alpaca_orders import OrderNotYetVisibleError
from money_pit.compute.fills import build_fill_observation
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.constants import RECOVERY_JSON_FILENAME
from money_pit.graph.state import PipelineState
from money_pit.pipeline.recovery import _NOT_FOUND_STATUS
from money_pit.pipeline.recovery import _UNOBSERVABLE_STATUS
from money_pit.pipeline.recovery import _build_halt_email
from money_pit.pipeline.recovery import _build_notice_email
from money_pit.pipeline.recovery import _find_prior_journal
from money_pit.pipeline.recovery import make_recovery_node
from money_pit.pipeline.recovery import reconcile_prior_run
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.enums import RecoveryDecision
from money_pit.schemas.fills import FillObservation
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.journal import ExecutionJournalEntry
from money_pit.schemas.recovery import PriorRunReconciliation
from money_pit.schemas.recovery import ReconciledOrder


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


class _CapturingEmail:
    """A real EmailSender that records every (subject, body) call so a test can assert on it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, subject: str, body: str) -> None:
        self.calls.append((subject, body))


def _make_state(working_dir: Path, slug: str) -> PipelineState:
    working_dir.mkdir(parents=True, exist_ok=True)
    return {"slug": slug, "working_dir": str(working_dir), "completed_steps": []}


def _read_recovery_record(working_dir: Path) -> PriorRunReconciliation:
    return PriorRunReconciliation.model_validate_json(
        (working_dir / RECOVERY_JSON_FILENAME).read_text(encoding="utf-8")
    )


def test_make_recovery_node_with_no_prior_run(tmp_path: Path) -> None:
    working_dir = tmp_path / CURRENT_SLUG
    state = _make_state(working_dir, CURRENT_SLUG)
    email = _CapturingEmail()
    node = make_recovery_node(_ScriptedObserver({}), email)

    result = node(state)

    assert result["recovery_decision"] is RecoveryDecision.PROCEED
    assert email.calls == []
    assert "recovery" in (result.get("completed_steps") or [])
    record = _read_recovery_record(working_dir)
    assert record.decision is RecoveryDecision.PROCEED


def test_make_recovery_node_with_prior_open_leg_now_settled(tmp_path: Path) -> None:
    entry = _make_entry(PRIOR_SLUG, "s1", ExecutionPhase.SUBMITTED)
    _write_journal(tmp_path, PRIOR_SLUG, ExecutionOutcome.EXECUTED_INCOMPLETE, [entry])
    working_dir = tmp_path / CURRENT_SLUG
    state = _make_state(working_dir, CURRENT_SLUG)
    email = _CapturingEmail()
    observer = _ScriptedObserver({entry.client_order_id: build_fill_observation("filled", 8.0, 100.0)})
    node = make_recovery_node(observer, email)

    result = node(state)

    assert result["recovery_decision"] is RecoveryDecision.PROCEED_WITH_NOTICE
    assert len(email.calls) == 1
    subject, body = email.calls[0]
    assert "Prior Run Reconciled" in subject
    assert entry.client_order_id in body
    record = _read_recovery_record(working_dir)
    assert record.decision is RecoveryDecision.PROCEED_WITH_NOTICE


def test_make_recovery_node_with_prior_open_leg_still_open(tmp_path: Path) -> None:
    entry = _make_entry(PRIOR_SLUG, "s1", ExecutionPhase.SUBMITTED)
    _write_journal(tmp_path, PRIOR_SLUG, ExecutionOutcome.EXECUTED_INCOMPLETE, [entry])
    working_dir = tmp_path / CURRENT_SLUG
    state = _make_state(working_dir, CURRENT_SLUG)
    email = _CapturingEmail()
    observer = _ScriptedObserver({entry.client_order_id: build_fill_observation("accepted", None, None)})
    node = make_recovery_node(observer, email)

    result = node(state)

    assert result["recovery_decision"] is RecoveryDecision.HALT
    assert len(email.calls) == 1
    subject, body = email.calls[0]
    assert "Recovery Halt" in subject
    assert entry.client_order_id in body
    record = _read_recovery_record(working_dir)
    assert record.decision is RecoveryDecision.HALT


def _make_reconciliation(
    decision: RecoveryDecision,
    prior_outcome: ExecutionOutcome | None,
    open_orders: list[ReconciledOrder],
    settled_orders: list[ReconciledOrder],
) -> PriorRunReconciliation:
    return PriorRunReconciliation(
        decision=decision,
        prior_slug=PRIOR_SLUG,
        prior_outcome=prior_outcome,
        open_orders=open_orders,
        settled_orders=settled_orders,
    )


def _make_reconciled_order(step_id: str, observed_status: str, phase: ExecutionPhase) -> ReconciledOrder:
    return ReconciledOrder(
        step_id=step_id,
        client_order_id=f"{PRIOR_SLUG}:{step_id}",
        observed_status=observed_status,
        phase=phase,
    )


def test__build_halt_email() -> None:
    orders = [
        _make_reconciled_order("s1", "accepted", ExecutionPhase.SUBMITTED),
        _make_reconciled_order("s2", "partially_filled", ExecutionPhase.PARTIALLY_FILLED),
    ]
    recon = _make_reconciliation(RecoveryDecision.HALT, ExecutionOutcome.EXECUTED_INCOMPLETE, orders, [])

    subject, body = _build_halt_email(CURRENT_SLUG, recon)

    assert subject == f"money-pit: Recovery Halt - {CURRENT_SLUG}"
    for order in orders:
        assert order.step_id in body
        assert order.client_order_id in body
        assert order.observed_status in body


def test__build_halt_email_with_unrecorded_prior_outcome() -> None:
    orders = [_make_reconciled_order("s1", "accepted", ExecutionPhase.SUBMITTED)]
    recon = _make_reconciliation(RecoveryDecision.HALT, None, orders, [])

    _, body = _build_halt_email(CURRENT_SLUG, recon)

    assert "Prior outcome: none (unrecorded)" in body
    assert "Prior outcome: None" not in body


def test__build_notice_email() -> None:
    orders = [
        _make_reconciled_order("s1", "filled", ExecutionPhase.FILLED),
        _make_reconciled_order("s2", _NOT_FOUND_STATUS, ExecutionPhase.SUBMITTED),
    ]
    recon = _make_reconciliation(RecoveryDecision.PROCEED_WITH_NOTICE, ExecutionOutcome.EXECUTED_INCOMPLETE, [], orders)

    subject, body = _build_notice_email(CURRENT_SLUG, recon)

    assert subject == f"money-pit: Prior Run Reconciled - {CURRENT_SLUG}"
    for order in orders:
        assert order.step_id in body
        assert order.client_order_id in body
        assert order.observed_status in body


def test__build_notice_email_with_unrecorded_prior_outcome() -> None:
    orders = [_make_reconciled_order("s1", "filled", ExecutionPhase.FILLED)]
    recon = _make_reconciliation(RecoveryDecision.PROCEED_WITH_NOTICE, None, [], orders)

    _, body = _build_notice_email(CURRENT_SLUG, recon)

    assert "Prior outcome: none (unrecorded)" in body
    assert "Prior outcome: None" not in body
