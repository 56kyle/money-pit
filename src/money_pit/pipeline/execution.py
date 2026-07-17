"""Module containing the execution sub-agent for independent-path order submission with a crash-survivable journal in the money_pit package."""

import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path

from pydantic import TypeAdapter

from money_pit.alpaca_orders import FillObservationError
from money_pit.alpaca_orders import OrderNotYetVisibleError
from money_pit.compute.fills import build_fill_observation
from money_pit.compute.fills import derive_execution_outcome
from money_pit.compute.fills import is_terminal_status
from money_pit.constants import ACTION_STEPS_JSON_FILENAME
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.contracts import FillObserver
from money_pit.contracts import OrderPlacer
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.fills import FillObservation
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.journal import ExecutionJournalEntry


_action_steps_adapter: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])


class OrderSubmissionError(Exception):
    """Raised by the injected place_order when the broker rejects a submission.

    This is the contracted failure boundary: place_order signals a rejected order (bad
    parameters, insufficient buying power, market-closed, broker outage) by raising this,
    distinct from an unexpected bug in our own code, which raises anything else and must
    propagate. The node catches only this type and fails the affected leg closed while
    continuing the run.
    """


class AtomicGroupNotSupportedError(Exception):
    """Raised when an action step carries a non-null group_id (see ADR 0003).

    Atomic-group (all-or-nothing) execution is deferred until an interdependent thesis
    requires it (a non-null group_id, which A4 never emits at N=1) and the open design
    decisions in architecture.md §15 #9 (grouping criteria), #10 (leg-execution ordering),
    and #11 (compensation cost bound) are settled. Rather than execute one leg of a group
    and leave exposure nobody chose, the node fails closed before placing any order. This
    is the marked terminus of the stub.
    """


def _write_journal(
    working_dir: Path, slug: str, entries: list[ExecutionJournalEntry], outcome: ExecutionOutcome | None
) -> None:
    journal: ExecutionJournal = ExecutionJournal(slug=slug, outcome=outcome, entries=entries)
    _ = (working_dir / EXECUTION_JOURNAL_FILENAME).write_text(journal.model_dump_json(indent=2), encoding="utf-8")


def _reject_atomic_groups(steps: list[ActionStep]) -> None:
    """Fail closed before placing any order when a step carries a non-null group_id (ADR 0003)."""
    if any(step.group_id is not None for step in steps):
        raise AtomicGroupNotSupportedError(
            "Atomic-group execution (non-null group_id) is not supported (ADR 0003)."
        )


def _submit_step(
    step: ActionStep, place_order: Callable[[ExecutionParameters], str]
) -> ExecutionJournalEntry:
    """Submit one independent leg, failing it closed on OrderSubmissionError and letting any other error propagate."""
    intended: dict[str, object] = step.execution_parameters.to_order_payload()
    phase: ExecutionPhase = ExecutionPhase.SUBMITTED
    broker_order_id: str | None = None
    error: str | None = None
    try:
        broker_order_id = place_order(step.execution_parameters)
    except OrderSubmissionError as exc:
        phase = ExecutionPhase.FAILED
        broker_order_id = None
        error = str(exc)
    return ExecutionJournalEntry(
        step_id=step.step_id,
        group_id=step.group_id,
        client_order_id=step.execution_parameters.client_order_id,
        phase=phase,
        intended=intended,
        broker_order_id=broker_order_id,
        status=None,
        filled_qty=None,
        filled_avg_price=None,
        realized_notional=None,
        compensation_of=None,
        error=error,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _unobserved_fill() -> FillObservation:
    """Return the honest SUBMITTED marker for an order that was sent but whose status was never observed."""
    return build_fill_observation("unobserved", None, None)


def _poll_fill(
    observe_fill: FillObserver,
    client_order_id: str,
    *,
    interval: float,
    timeout: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> FillObservation:
    """Poll observe_fill until the order reaches a terminal status or the timeout elapses, failing closed.

    A 404 (order not yet indexed) and a transient read error are both treated as retryable
    in-flight; if the order never reaches a terminal status within the window we record the
    last-observed non-terminal state (or an unobserved marker), never a fabricated fill.
    The clock is injected so the loop is deterministic and testable without real time.
    """
    deadline: float = monotonic() + timeout
    last: FillObservation | None = None
    while True:
        try:
            obs: FillObservation = observe_fill(client_order_id)
            last = obs
            if is_terminal_status(obs.status):
                return obs
        except (OrderNotYetVisibleError, FillObservationError):
            pass
        if monotonic() >= deadline:
            return last if last is not None else _unobserved_fill()
        sleep(interval)


def _apply_fill(entry: ExecutionJournalEntry, obs: FillObservation) -> ExecutionJournalEntry:
    """Return a copy of entry updated with the observed phase and fill fields."""
    return entry.model_copy(
        update={
            "phase": obs.phase,
            "status": obs.status,
            "filled_qty": obs.filled_qty,
            "filled_avg_price": obs.filled_avg_price,
            "realized_notional": obs.realized_notional,
        }
    )


def make_execution_node(
    place_order: OrderPlacer,
    observe_fill: FillObserver,
    *,
    poll_interval: float,
    poll_timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> PipelineNode:
    """Return a LangGraph node that submits validated orders and journals their observed fills.

    place_order is contracted to return a broker order id on success and to raise
    OrderSubmissionError on a broker rejection; any other exception is treated as an
    unexpected bug and propagates, leaving a truthful partial journal on disk.

    After a successful submission each order's real fill is observed by polling observe_fill
    until a terminal status or poll_timeout, then the true phase and fill fields are journaled.
    Polling fails closed: an order that never reaches a terminal status is recorded as its
    last-observed non-terminal state (or an unobserved marker), never a fabricated fill. Every
    submission and every observation is written to disk incrementally so a crash mid-run leaves
    a truthful journal.
    """

    def execution_node(state: PipelineState) -> PipelineState:
        slug: str = require_slug(state)
        working_dir: Path = require_working_dir(state)

        steps: list[ActionStep] = _action_steps_adapter.validate_json(
            (working_dir / ACTION_STEPS_JSON_FILENAME).read_text(encoding="utf-8")
        )
        _reject_atomic_groups(steps)

        entries: list[ExecutionJournalEntry] = []
        for step in steps:
            entry: ExecutionJournalEntry = _submit_step(step, place_order)
            entries.append(entry)
            _write_journal(working_dir, slug, entries, outcome=None)
            if entry.phase == ExecutionPhase.SUBMITTED:
                obs: FillObservation = _poll_fill(
                    observe_fill,
                    step.execution_parameters.client_order_id,
                    interval=poll_interval,
                    timeout=poll_timeout,
                    sleep=sleep,
                    monotonic=monotonic,
                )
                entries[-1] = _apply_fill(entry, obs)
                _write_journal(working_dir, slug, entries, outcome=None)

        outcome: ExecutionOutcome = derive_execution_outcome([e.phase for e in entries])
        _write_journal(working_dir, slug, entries, outcome=outcome)

        result: PipelineState = {
            "completed_steps": with_completed_step(state, "execution"),
            "execution_outcome": outcome,
        }
        return result

    return execution_node
