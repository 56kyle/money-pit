"""Module containing the Agent 6 pure determination seam plus the determination and finalizer graph nodes for the money_pit package.

The recompute/load/map functions are pure and directly testable; the two node factories
compose them into the graph. The determination node decides PROCEED/HALT (or fails closed to
ORCHESTRATION_ERROR) from the persisted validation artifact and routes; the finalizer node
writes the single determination.json/.md audit record once, after the chosen sub-agent has run.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Literal

from loguru import logger
from pydantic import ValidationError

from money_pit.constants import ACTION_STEPS_VALIDATION_JSON_FILENAME
from money_pit.constants import DETERMINATION_JSON_FILENAME
from money_pit.constants import DETERMINATION_MD_FILENAME
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.schemas.determination import DeterminationReport
from money_pit.schemas.enums import Determination
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.validation_results import ActionStepsValidation


_SUB_AGENT_EXECUTION: Literal["execution"] = "execution"
_SUB_AGENT_NOTIFICATION: Literal["notification"] = "notification"

_PROCEED_REASON: str = "All validation steps matched; execution authorised."
_PARSE_FAILURE_REASON: str = "Validation artifact could not be parsed; orchestration error."

_SUCCESS: Literal["success"] = "success"
_FAILURE: Literal["failure"] = "failure"

_SUCCESS_OUTCOMES: frozenset[ExecutionOutcome] = frozenset(
    {ExecutionOutcome.EXECUTED_CLEAN, ExecutionOutcome.PARTIAL_COMPENSATED}
)


class DeterminationParseError(Exception):
    """Raised when the persisted validation artifact is missing, unreadable, or malformed.

    A6 cannot answer PROCEED/HALT without a well-formed action_steps_validation.json with at
    least one step; this failure routes fail-closed to ORCHESTRATION_ERROR (no sub-agent).
    """


def recompute_determination(validation: ActionStepsValidation) -> Determination:
    """Return PROCEED when every step MATCHED, HALT on any UNMATCHED; raise on empty steps."""
    if not validation.steps:
        raise DeterminationParseError("Validation artifact has no steps to evaluate.")
    if all(step.status == ValidationStatus.MATCHED for step in validation.steps):
        return Determination.PROCEED
    return Determination.HALT


def load_validation(working_dir: Path) -> ActionStepsValidation:
    """Load action_steps_validation.json, failing closed on missing/unreadable/schema-invalid."""
    path: Path = working_dir / ACTION_STEPS_VALIDATION_JSON_FILENAME
    if not path.exists():
        raise DeterminationParseError(f"Validation artifact not found: {path}.")
    try:
        return ActionStepsValidation.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise DeterminationParseError(f"Validation artifact at {path} is malformed.") from error


def map_execution_outcome(outcome: ExecutionOutcome | None) -> Literal["success", "failure"]:
    """Map an execution outcome to success/failure, treating an incomplete journal as failure."""
    if outcome in _SUCCESS_OUTCOMES:
        return _SUCCESS
    return _FAILURE


def _failed_step_ids(validation: ActionStepsValidation) -> list[str]:
    return [step.step_id for step in validation.steps if step.status == ValidationStatus.UNMATCHED]


def _halt_reason(failed_steps: list[str]) -> str:
    return f"{len(failed_steps)} validation step(s) unmatched; execution halted."


def make_determination_node() -> PipelineNode:
    """Return the node that recomputes the go/no-go from the persisted validation and routes."""

    def determination_node(state: PipelineState) -> PipelineState:
        working_dir: Path = require_working_dir(state)
        completed: list[str] = with_completed_step(state, "determination")

        try:
            validation: ActionStepsValidation = load_validation(working_dir)
            determination: Determination = recompute_determination(validation)
        except DeterminationParseError:
            return {
                "terminal_state": TerminalState.ORCHESTRATION_ERROR,
                "determination": Determination.HALT,
                "failed_steps": [],
                "sub_agent_spawned": None,
                "determination_reason": _PARSE_FAILURE_REASON,
                "completed_steps": completed,
            }

        if determination == Determination.PROCEED:
            return {
                "determination": Determination.PROCEED,
                "failed_steps": [],
                "sub_agent_spawned": _SUB_AGENT_EXECUTION,
                "determination_reason": _PROCEED_REASON,
                "completed_steps": completed,
            }

        failed_steps: list[str] = _failed_step_ids(validation)
        return {
            "terminal_state": TerminalState.VALIDATION_ERROR,
            "determination": Determination.HALT,
            "failed_steps": failed_steps,
            "sub_agent_spawned": _SUB_AGENT_NOTIFICATION,
            "determination_reason": _halt_reason(failed_steps),
            "completed_steps": completed,
        }

    return determination_node


def _read_journal_outcome(working_dir: Path) -> ExecutionOutcome | None:
    """Return the journal outcome, or None when absent; logs at ERROR on a corrupt journal."""
    path: Path = working_dir / EXECUTION_JOURNAL_FILENAME
    if not path.exists():
        return None
    try:
        journal: ExecutionJournal = ExecutionJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        logger.error(
            "Execution journal at {path} is corrupt; treating outcome as absent: {error}",
            path=path,
            error=error,
        )
        return None
    return journal.outcome


def _resolve_sub_agent_outcome(
    sub_agent_spawned: str | None, working_dir: Path
) -> Literal["success", "failure"] | None:
    if sub_agent_spawned == _SUB_AGENT_EXECUTION:
        return map_execution_outcome(_read_journal_outcome(working_dir))
    if sub_agent_spawned == _SUB_AGENT_NOTIFICATION:
        return _SUCCESS
    return None


def _render_report_md(report: DeterminationReport) -> str:
    lines: list[str] = [
        f"# Determination — {report.slug}",
        "",
        f"Determination: {report.determination.value}",
        f"Reason: {report.reason}",
        f"Sub-agent spawned: {report.sub_agent_spawned or 'none'}",
        f"Sub-agent outcome: {report.sub_agent_outcome or 'none'}",
    ]
    if report.failed_steps:
        lines.append("")
        lines.append(f"Failed steps ({len(report.failed_steps)}):")
        lines.extend(f"  {step_id}" for step_id in report.failed_steps)
    return "\n".join(lines) + "\n"


def _write_determination_report(working_dir: Path, report: DeterminationReport) -> None:
    _ = (working_dir / DETERMINATION_JSON_FILENAME).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    _ = (working_dir / DETERMINATION_MD_FILENAME).write_text(_render_report_md(report), encoding="utf-8")


def make_finalizer_node() -> PipelineNode:
    """Return the node that writes the single determination.json/.md record after the sub-agent runs."""

    def finalizer_node(state: PipelineState) -> PipelineState:
        slug: str = require_slug(state)
        working_dir: Path = require_working_dir(state)

        determination: Determination | None = state.get("determination")
        if determination is None:
            raise ValueError("PipelineState missing required key 'determination' at finalizer")
        sub_agent_spawned: Literal["execution", "notification"] | None = state.get("sub_agent_spawned")

        report: DeterminationReport = DeterminationReport(
            slug=slug,
            determination=determination,
            reason=state.get("determination_reason", ""),
            failed_steps=list(state.get("failed_steps") or []),
            sub_agent_spawned=sub_agent_spawned,
            sub_agent_outcome=_resolve_sub_agent_outcome(sub_agent_spawned, working_dir),
            sub_agent_error=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        _write_determination_report(working_dir, report)

        return {
            "completed_steps": with_completed_step(state, "finalizer"),
        }

    return finalizer_node
