"""Module containing the notification sub-agent for per-terminal-state email templating and the send_email call in the money_pit package."""

from collections.abc import Callable
from pathlib import Path

from pydantic import TypeAdapter

from money_pit.constants import ACTION_STEPS_JSON_FILENAME
from money_pit.constants import ACTION_STEPS_VALIDATION_JSON_FILENAME
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.validation_results import ActionStepsValidation


_action_steps_ta: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])


def _build_subject(
    slug: str,
    terminal_state: TerminalState | None,
    execution_outcome: ExecutionOutcome | None,
    _validation: ActionStepsValidation | None,
) -> str:
    if terminal_state is TerminalState.ANALYSIS_HALT:
        return f"money-pit: Analysis Halt - {slug}"
    if terminal_state is TerminalState.VALIDATION_ERROR:
        return f"money-pit: MCP Validation Error - {slug}"
    if terminal_state is None and execution_outcome is ExecutionOutcome.EXECUTED_INCOMPLETE:
        return f"money-pit: Execution Incomplete - {slug}"
    raise ValueError(
        f"_build_subject received unexpected terminal state / execution outcome: "
        f"{terminal_state} / {execution_outcome}"
    )


def _build_execution_incomplete_body(
    slug: str,
    execution_outcome: ExecutionOutcome,
    execution_journal: ExecutionJournal | None,
) -> str:
    lines: list[str] = [
        f"Slug: {slug}",
        f"Execution outcome: {execution_outcome.value}",
        "",
    ]
    if execution_journal is not None and execution_journal.entries:
        lines.append(f"Execution journal ({len(execution_journal.entries)}):")
        for entry in execution_journal.entries:
            lines.append(
                f"  {entry.step_id} | {entry.phase.value} | {entry.status} | filled_qty={entry.filled_qty}"
            )
    else:
        lines.append("Execution journal: none")
    return "\n".join(lines)


def _build_body(
    slug: str,
    terminal_state: TerminalState | None,
    execution_outcome: ExecutionOutcome | None,
    action_steps: list[ActionStep],
    validation: ActionStepsValidation | None,
    execution_journal: ExecutionJournal | None,
) -> str:
    if terminal_state is None and execution_outcome is ExecutionOutcome.EXECUTED_INCOMPLETE:
        return _build_execution_incomplete_body(slug, execution_outcome, execution_journal)

    terminal_label: str = terminal_state.value if terminal_state is not None else "none (validation gate)"
    lines: list[str] = [
        f"Slug: {slug}",
        f"Terminal state: {terminal_label}",
    ]

    lines.append("")
    if action_steps:
        lines.append(f"Action steps ({len(action_steps)}):")
        for step in action_steps:
            lines.append(
                f"  {step.step_id} | {step.instrument} | {step.action_type.value} | {step.one_sentence_thesis}"
            )
    else:
        lines.append("Action steps: none")

    if validation is not None:
        unmatched = [s for s in validation.steps if s.status == ValidationStatus.UNMATCHED]
        if unmatched:
            lines.append("")
            lines.append(f"Unmatched validation steps ({len(unmatched)}):")
            for step in unmatched:
                gap: str = step.gap_description or "(no gap description)"
                lines.append(f"  {step.step_id}: {gap}")

    return "\n".join(lines)


def _load_notification_inputs(
    working_dir: Path,
) -> tuple[list[ActionStep], ActionStepsValidation | None]:
    """Load the optional action-steps and validation artifacts, defaulting each when absent."""
    action_steps: list[ActionStep] = []
    if (action_steps_path := working_dir / ACTION_STEPS_JSON_FILENAME).exists():
        action_steps = _action_steps_ta.validate_json(action_steps_path.read_text(encoding="utf-8"))

    validation: ActionStepsValidation | None = None
    if (validation_path := working_dir / ACTION_STEPS_VALIDATION_JSON_FILENAME).exists():
        validation = ActionStepsValidation.model_validate_json(validation_path.read_text(encoding="utf-8"))

    return action_steps, validation


def _load_execution_journal(working_dir: Path) -> ExecutionJournal | None:
    """Load the optional execution journal, defaulting to None when absent."""
    if (journal_path := working_dir / EXECUTION_JOURNAL_FILENAME).exists():
        return ExecutionJournal.model_validate_json(journal_path.read_text(encoding="utf-8"))
    return None


def make_notification_node(
    send_email: Callable[[str, str], None],
) -> PipelineNode:
    """Return a LangGraph node that sends an email summary when execution does not proceed."""

    def notification_node(state: PipelineState) -> PipelineState:
        slug: str = require_slug(state)
        working_dir: Path = require_working_dir(state)
        terminal_state: TerminalState | None = state.get("terminal_state")
        execution_outcome: ExecutionOutcome | None = state.get("execution_outcome")

        action_steps, validation = _load_notification_inputs(working_dir)
        execution_journal: ExecutionJournal | None = _load_execution_journal(working_dir)

        subject: str = _build_subject(slug, terminal_state, execution_outcome, validation)
        body: str = _build_body(
            slug, terminal_state, execution_outcome, action_steps, validation, execution_journal
        )
        send_email(subject, body)

        return {"completed_steps": with_completed_step(state, "notification")}

    return notification_node
