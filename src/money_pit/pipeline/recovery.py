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
from money_pit.constants import RECOVERY_JSON_FILENAME
from money_pit.contracts import EmailSender
from money_pit.contracts import FillObserver
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
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


def _format_orders(orders: list[ReconciledOrder]) -> list[str]:
    """Render each reconciled leg as an aligned step_id | client_order_id | observed_status line."""
    return [f"  {order.step_id} | {order.client_order_id} | {order.observed_status}" for order in orders]


def _prior_outcome_label(prior_outcome: ExecutionOutcome | None) -> str:
    """Render a prior run's outcome, naming the unrecorded (crashed mid-run) case explicitly."""
    return prior_outcome.value if prior_outcome is not None else "none (unrecorded)"


def _build_halt_email(slug: str, recon: PriorRunReconciliation) -> tuple[str, str]:
    """Build the (subject, body) for a new run halted because a prior leg is still open at the broker."""
    subject: str = f"money-pit: Recovery Halt - {slug}"
    lines: list[str] = [
        f"Slug: {slug}",
        f"Prior run: {recon.prior_slug}",
        f"Prior outcome: {_prior_outcome_label(recon.prior_outcome)}",
        "",
        "A prior run has orders STILL OPEN at the broker. This run was halted before",
        "planning to avoid doubling the exposure the open legs already carry.",
        "",
        f"Open orders ({len(recon.open_orders)}):",
        *_format_orders(recon.open_orders),
    ]
    return subject, "\n".join(lines)


def _build_notice_email(slug: str, recon: PriorRunReconciliation) -> tuple[str, str]:
    """Build the (subject, body) for a run proceeding after a prior abnormal run has since settled."""
    subject: str = f"money-pit: Prior Run Reconciled - {slug}"
    lines: list[str] = [
        f"Slug: {slug}",
        f"Prior run: {recon.prior_slug}",
        f"Prior outcome: {_prior_outcome_label(recon.prior_outcome)}",
        "",
        "A prior abnormal run has since settled at the broker. This run proceeds.",
        "",
        f"Settled orders ({len(recon.settled_orders)}):",
        *_format_orders(recon.settled_orders),
    ]
    return subject, "\n".join(lines)


def _write_recovery_record(working_dir: Path, recon: PriorRunReconciliation) -> None:
    """Write the recovery reconciliation verdict to the run's recovery.json artifact."""
    _ = (working_dir / RECOVERY_JSON_FILENAME).write_text(recon.model_dump_json(indent=2), encoding="utf-8")


def make_recovery_node(observe_fill: FillObserver, send_email: EmailSender) -> PipelineNode:
    """Return the LangGraph entry node that reconciles the most recent prior run before this run plans.

    A prior leg still open at the broker halts this run and emails the owner; an abnormal prior
    run that has since settled emails a reconciliation notice and proceeds; anything else proceeds
    silently. The verdict is always written to recovery.json. These emails are recovery's own
    concern and are sent directly, not routed through the notification node.
    """

    def recovery_node(state: PipelineState) -> PipelineState:
        working_dir: Path = require_working_dir(state)
        slug: str = require_slug(state)
        daily_show_root: Path = working_dir.parent

        recon: PriorRunReconciliation = reconcile_prior_run(daily_show_root, slug, observe_fill)
        _write_recovery_record(working_dir, recon)

        if recon.decision is RecoveryDecision.HALT:
            send_email(*_build_halt_email(slug, recon))
        elif recon.decision is RecoveryDecision.PROCEED_WITH_NOTICE:
            send_email(*_build_notice_email(slug, recon))

        return {
            "recovery_decision": recon.decision,
            "completed_steps": with_completed_step(state, "recovery"),
        }

    return recovery_node
