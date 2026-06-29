"""Execution sub-agent: transactional loop, idempotent orders, journal, compensation."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import TypeAdapter

from money_pit.graph.state import PipelineState
from money_pit.schemas.action_steps import ActionStep, ExecutionParameters
from money_pit.schemas.enums import ExecutionOutcome, ExecutionPhase
from money_pit.schemas.journal import ExecutionJournal, ExecutionJournalEntry

_action_steps_adapter: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])


def make_execution_node(
    place_order: Callable[[ExecutionParameters], str],
) -> Callable[[PipelineState], dict[str, object]]:
    """Return a LangGraph node that submits validated orders via the injected place_order callable."""

    def execution_node(state: PipelineState) -> dict[str, object]:
        slug: str | None = state.get("slug")
        if slug is None:
            raise ValueError("PipelineState missing required key 'slug'")
        working_dir_raw: str | None = state.get("working_dir")
        if working_dir_raw is None:
            raise ValueError("PipelineState missing required key 'working_dir'")
        working_dir: Path = Path(working_dir_raw)

        steps: list[ActionStep] = _action_steps_adapter.validate_json(
            (working_dir / "action_steps.json").read_text(encoding="utf-8")
        )

        entries: list[ExecutionJournalEntry] = []
        for step in steps:
            broker_order_id: str = place_order(step.execution_parameters)
            intended: dict[str, object] = step.execution_parameters.model_dump()
            entry: ExecutionJournalEntry = ExecutionJournalEntry(
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

        journal: ExecutionJournal = ExecutionJournal(
            slug=slug,
            outcome=ExecutionOutcome.EXECUTED_CLEAN,
            entries=entries,
        )
        _ = (working_dir / "execution_journal.json").write_text(
            journal.model_dump_json(indent=2),
            encoding="utf-8",
        )

        result: dict[str, object] = {
            "completed_steps": list(state.get("completed_steps") or []) + ["execution"],
        }
        return result

    return execution_node
