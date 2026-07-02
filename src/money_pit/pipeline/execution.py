"""Execution sub-agent: independent-path order submission with a crash-survivable journal."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import TypeAdapter

from money_pit.constants import ACTION_STEPS_JSON_FILENAME
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.graph.state import PipelineState
from money_pit.schemas.action_steps import ActionStep, ExecutionParameters
from money_pit.schemas.enums import ExecutionOutcome, ExecutionPhase
from money_pit.schemas.journal import ExecutionJournal, ExecutionJournalEntry

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

    Atomic-group (all-or-nothing) execution is the deferred Phase-7 stub; at N=1 every step
    is independent. Rather than execute one leg of a group and leave exposure nobody chose,
    the node fails closed before placing any order. This is the marked terminus of the stub.
    """


def _derive_outcome(entries: list[ExecutionJournalEntry]) -> ExecutionOutcome:
    """Return EXECUTION_FAILED if any entry failed, else EXECUTED_CLEAN."""
    if any(entry.phase == ExecutionPhase.FAILED for entry in entries):
        return ExecutionOutcome.EXECUTION_FAILED
    return ExecutionOutcome.EXECUTED_CLEAN


def _write_journal(
    working_dir: Path, slug: str, entries: list[ExecutionJournalEntry], outcome: ExecutionOutcome | None
) -> None:
    journal: ExecutionJournal = ExecutionJournal(slug=slug, outcome=outcome, entries=entries)
    _ = (working_dir / EXECUTION_JOURNAL_FILENAME).write_text(
        journal.model_dump_json(indent=2), encoding="utf-8"
    )


def make_execution_node(
    place_order: Callable[[ExecutionParameters], str],
) -> Callable[[PipelineState], dict[str, object]]:
    """Return a LangGraph node that submits validated orders via the injected place_order callable.

    place_order is contracted to return a broker order id on success and to raise
    OrderSubmissionError on a broker rejection; any other exception is treated as an
    unexpected bug and propagates, leaving a truthful partial journal on disk.
    """

    def execution_node(state: PipelineState) -> dict[str, object]:
        slug: str | None = state.get("slug")
        if slug is None:
            raise ValueError("PipelineState missing required key 'slug'")
        working_dir_raw: str | None = state.get("working_dir")
        if working_dir_raw is None:
            raise ValueError("PipelineState missing required key 'working_dir'")
        working_dir: Path = Path(working_dir_raw)

        steps: list[ActionStep] = _action_steps_adapter.validate_json(
            (working_dir / ACTION_STEPS_JSON_FILENAME).read_text(encoding="utf-8")
        )

        if any(step.group_id is not None for step in steps):
            raise AtomicGroupNotSupportedError(
                "Atomic-group execution (non-null group_id) is not supported pre-Phase-7 (ADR 0003)."
            )

        entries: list[ExecutionJournalEntry] = []
        for step in steps:
            intended: dict[str, object] = step.execution_parameters.to_order_payload()
            try:
                broker_order_id: str = place_order(step.execution_parameters)
            except OrderSubmissionError as exc:
                entry: ExecutionJournalEntry = ExecutionJournalEntry(
                    step_id=step.step_id,
                    group_id=step.group_id,
                    client_order_id=step.execution_parameters.client_order_id,
                    phase=ExecutionPhase.FAILED,
                    intended=intended,
                    broker_order_id=None,
                    status=None,
                    filled_qty=None,
                    filled_avg_price=None,
                    realized_notional=None,
                    compensation_of=None,
                    error=str(exc),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            else:
                entry = ExecutionJournalEntry(
                    step_id=step.step_id,
                    group_id=step.group_id,
                    client_order_id=step.execution_parameters.client_order_id,
                    phase=ExecutionPhase.SUBMITTED,
                    intended=intended,
                    broker_order_id=broker_order_id,
                    status=None,
                    filled_qty=None,
                    filled_avg_price=None,
                    realized_notional=None,
                    compensation_of=None,
                    error=None,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            entries.append(entry)
            _write_journal(working_dir, slug, entries, outcome=None)

        _write_journal(working_dir, slug, entries, outcome=_derive_outcome(entries))

        result: dict[str, object] = {
            "completed_steps": list(state.get("completed_steps") or []) + ["execution"],
        }
        return result

    return execution_node
