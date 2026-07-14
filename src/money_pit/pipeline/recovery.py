"""Module containing prior-run reconciliation that re-observes potentially-open prior legs before a new run plans for the money_pit package.

At a new run's start the most recent prior run's execution journal is reconciled against
the broker. Filled or terminal legs need no action; the fresh snapshot already reflects
them and re-planning from reality handles them. The one correctness risk is a prior order
still open at the broker now: it is not yet a position, so the snapshot misses it, and the
new run would double the exposure. Any potentially-open prior leg is therefore re-observed;
an unresolved open leg halts the new run, an all-settled abnormal prior run proceeds with a
notice, and everything else proceeds silently.
"""

from collections.abc import Iterator
from pathlib import Path

from money_pit.alpaca_orders import FillObservationError
from money_pit.alpaca_orders import OrderNotYetVisibleError
from money_pit.compute.fills import is_terminal_status
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.contracts import FillObserver
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.enums import RecoveryDecision
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.journal import ExecutionJournalEntry
from money_pit.schemas.recovery import PriorRunReconciliation
from money_pit.schemas.recovery import ReconciledOrder


_POTENTIALLY_OPEN_PHASES: frozenset[ExecutionPhase] = frozenset(
    {ExecutionPhase.SUBMITTED, ExecutionPhase.PARTIALLY_FILLED}
)

_ABNORMAL_PRIOR_OUTCOMES: frozenset[ExecutionOutcome | None] = frozenset(
    {ExecutionOutcome.EXECUTED_INCOMPLETE, None}
)

_NOT_FOUND_STATUS: str = "not_found"
_UNOBSERVABLE_STATUS: str = "unobservable"


def _prior_slug_candidates(daily_show_root: Path, current_slug: str) -> Iterator[Path]:
    """Yield run directories strictly earlier than current_slug that hold an execution journal, newest first."""
    if not daily_show_root.is_dir():
        return
    earlier: list[Path] = sorted(
        (
            run_dir
            for run_dir in daily_show_root.iterdir()
            if run_dir.is_dir()
            and run_dir.name < current_slug
            and (run_dir / EXECUTION_JOURNAL_FILENAME).is_file()
        ),
        key=lambda run_dir: run_dir.name,
        reverse=True,
    )
    yield from earlier


def _find_prior_journal(daily_show_root: Path, current_slug: str) -> Path | None:
    """Return the execution journal path of the most recent prior run, or None when none has one."""
    for run_dir in _prior_slug_candidates(daily_show_root, current_slug):
        return run_dir / EXECUTION_JOURNAL_FILENAME
    return None


def _reobserve(entry: ExecutionJournalEntry, observe_fill: FillObserver) -> ReconciledOrder:
    """Build a ReconciledOrder from the leg's current broker-observed fill state."""
    observation = observe_fill(entry.client_order_id)
    return ReconciledOrder(
        step_id=entry.step_id,
        client_order_id=entry.client_order_id,
        observed_status=observation.status,
        phase=observation.phase,
    )


def _sentinel_order(entry: ExecutionJournalEntry, observed_status: str) -> ReconciledOrder:
    """Build a ReconciledOrder for a leg whose current status could not be read as a real order."""
    return ReconciledOrder(
        step_id=entry.step_id,
        client_order_id=entry.client_order_id,
        observed_status=observed_status,
        phase=entry.phase,
    )


def _decide(
    open_orders: list[ReconciledOrder],
    prior_outcome: ExecutionOutcome | None,
) -> RecoveryDecision:
    """Halt on any still-open leg, notice on an all-settled abnormal prior run, else proceed."""
    if open_orders:
        return RecoveryDecision.HALT
    if prior_outcome in _ABNORMAL_PRIOR_OUTCOMES:
        return RecoveryDecision.PROCEED_WITH_NOTICE
    return RecoveryDecision.PROCEED


def reconcile_prior_run(
    daily_show_root: Path,
    current_slug: str,
    observe_fill: FillObserver,
) -> PriorRunReconciliation:
    """Reconcile the most recent prior run's potentially-open legs against the broker before the new run plans.

    Only legs recorded in a potentially-open phase are re-observed. settled_orders and
    open_orders therefore reflect just those re-observed legs; already-terminal prior legs
    are settled by construction and are not re-queried or listed.
    """
    journal_path: Path | None = _find_prior_journal(daily_show_root, current_slug)
    if journal_path is None:
        return PriorRunReconciliation(
            decision=RecoveryDecision.PROCEED,
            prior_slug=None,
            prior_outcome=None,
            open_orders=[],
            settled_orders=[],
        )

    journal: ExecutionJournal = ExecutionJournal.model_validate_json(
        journal_path.read_text(encoding="utf-8")
    )

    open_orders: list[ReconciledOrder] = []
    settled_orders: list[ReconciledOrder] = []
    for entry in journal.entries:
        if entry.phase not in _POTENTIALLY_OPEN_PHASES:
            continue
        try:
            reconciled: ReconciledOrder = _reobserve(entry, observe_fill)
        except OrderNotYetVisibleError:
            settled_orders.append(_sentinel_order(entry, _NOT_FOUND_STATUS))
            continue
        except FillObservationError:
            open_orders.append(_sentinel_order(entry, _UNOBSERVABLE_STATUS))
            continue
        if is_terminal_status(reconciled.observed_status):
            settled_orders.append(reconciled)
        else:
            open_orders.append(reconciled)

    return PriorRunReconciliation(
        decision=_decide(open_orders, journal.outcome),
        prior_slug=journal.slug,
        prior_outcome=journal.outcome,
        open_orders=open_orders,
        settled_orders=settled_orders,
    )
