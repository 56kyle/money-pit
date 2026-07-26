"""Module containing the Agent 6 pure determination seam plus the determination and finalizer graph nodes for the money_pit package.

The recompute/load/map functions are pure and directly testable; the two node factories
compose them into the graph. The determination node decides PROCEED/HALT (or fails closed to
ORCHESTRATION_ERROR) from the persisted validation artifact, persists that verdict to
determination_verdict.json and routes; the finalizer node writes the single determination.json/.md
audit record once, after the chosen sub-agent has run.
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
from money_pit.constants import DETERMINATION_VERDICT_JSON_FILENAME
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.schemas.determination import DeterminationReport
from money_pit.schemas.determination import DeterminationVerdict
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


class DeterminationVerdictError(Exception):
    """Raised when the finalizer can obtain no trustworthy determination verdict from disk or state.

    Covers a malformed determination_verdict.json, an absent one paired with a state that carries
    no determination, and one whose slug names a different run; either way there is no go/no-go
    that can be recorded for this run and the run cannot be concluded.
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


def load_verdict(working_dir: Path) -> DeterminationVerdict | None:
    """Return the persisted determination verdict, None when absent; raise on a malformed artifact."""
    path: Path = working_dir / DETERMINATION_VERDICT_JSON_FILENAME
    if not path.exists():
        return None
    try:
        return DeterminationVerdict.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise DeterminationVerdictError(f"Determination verdict at {path} is malformed.") from error


def _write_verdict(working_dir: Path, verdict: DeterminationVerdict) -> None:
    """Write the go/no-go handoff to the run's determination_verdict.json artifact."""
    _ = (working_dir / DETERMINATION_VERDICT_JSON_FILENAME).write_text(
        verdict.model_dump_json(indent=2), encoding="utf-8"
    )


def _failed_step_ids(validation: ActionStepsValidation) -> list[str]:
    return [step.step_id for step in validation.steps if step.status == ValidationStatus.UNMATCHED]


def _halt_reason(failed_steps: list[str]) -> str:
    return f"{len(failed_steps)} validation step(s) unmatched; execution halted."


def make_determination_node() -> PipelineNode:
    """Return the node that recomputes the go/no-go from the persisted validation, records it and routes.

    The verdict is written to determination_verdict.json on every outcome, orchestration failure
    included, so that a re-run against an existing run directory always leaves a truthful artifact
    and the finalizer never depends on in-memory state alone.
    """

    def determination_node(state: PipelineState) -> PipelineState:
        slug: str = require_slug(state)
        working_dir: Path = require_working_dir(state)
        completed: list[str] = with_completed_step(state, "determination")

        try:
            validation: ActionStepsValidation = load_validation(working_dir)
            determination: Determination = recompute_determination(validation)
        except DeterminationParseError:
            _write_verdict(
                working_dir,
                DeterminationVerdict(
                    slug=slug,
                    determination=Determination.HALT,
                    reason=_PARSE_FAILURE_REASON,
                    failed_steps=[],
                    sub_agent_spawned=None,
                ),
            )
            return {
                "terminal_state": TerminalState.ORCHESTRATION_ERROR,
                "determination": Determination.HALT,
                "failed_steps": [],
                "sub_agent_spawned": None,
                "determination_reason": _PARSE_FAILURE_REASON,
                "completed_steps": completed,
            }

        if determination == Determination.PROCEED:
            _write_verdict(
                working_dir,
                DeterminationVerdict(
                    slug=slug,
                    determination=Determination.PROCEED,
                    reason=_PROCEED_REASON,
                    failed_steps=[],
                    sub_agent_spawned=_SUB_AGENT_EXECUTION,
                ),
            )
            return {
                "determination": Determination.PROCEED,
                "failed_steps": [],
                "sub_agent_spawned": _SUB_AGENT_EXECUTION,
                "determination_reason": _PROCEED_REASON,
                "completed_steps": completed,
            }

        failed_steps: list[str] = _failed_step_ids(validation)
        halt_reason: str = _halt_reason(failed_steps)
        _write_verdict(
            working_dir,
            DeterminationVerdict(
                slug=slug,
                determination=Determination.HALT,
                reason=halt_reason,
                failed_steps=failed_steps,
                sub_agent_spawned=_SUB_AGENT_NOTIFICATION,
            ),
        )
        return {
            "terminal_state": TerminalState.VALIDATION_ERROR,
            "determination": Determination.HALT,
            "failed_steps": failed_steps,
            "sub_agent_spawned": _SUB_AGENT_NOTIFICATION,
            "determination_reason": halt_reason,
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


def _verdict_from_state(state: PipelineState, slug: str) -> DeterminationVerdict:
    """Rebuild the verdict from the in-memory state; raise when the state carries no determination."""
    determination: Determination | None = state.get("determination")
    if determination is None:
        raise DeterminationVerdictError(
            "No determination verdict on disk and PipelineState carries no 'determination' at finalizer."
        )
    return DeterminationVerdict(
        slug=slug,
        determination=determination,
        reason=state.get("determination_reason", ""),
        failed_steps=list(state.get("failed_steps") or []),
        sub_agent_spawned=state.get("sub_agent_spawned"),
    )


def resolve_verdict(state: PipelineState, working_dir: Path, slug: str) -> DeterminationVerdict:
    """Return the run's verdict from determination_verdict.json, else state; raise DeterminationVerdictError on a foreign slug.

    A persisted verdict carrying a different slug belongs to another run, so the working directory
    does not belong to this one; that is surfaced rather than resolved in favour of either source.
    """
    persisted: DeterminationVerdict | None = load_verdict(working_dir)
    if persisted is None:
        return _verdict_from_state(state, slug)
    if persisted.slug != slug:
        raise DeterminationVerdictError(
            f"Determination verdict in {working_dir} belongs to run {persisted.slug!r}, not {slug!r}."
        )
    return persisted


def make_finalizer_node() -> PipelineNode:
    """Return the node that writes the single determination.json/.md record after the sub-agent runs.

    The verdict is taken from determination_verdict.json when that artifact exists, so the node can
    run standalone against a run directory; the in-memory state is used only when the artifact is
    absent, which keeps a graph run concluding even if the determination node's write failed. Disk
    wins because it is what a re-run of determination updates. Agreement between the two is enforced
    by the verdict's slug: an artifact carrying another run's slug raises DeterminationVerdictError
    rather than falling back to state, so a stale go/no-go can never reach determination.json.
    """

    def finalizer_node(state: PipelineState) -> PipelineState:
        slug: str = require_slug(state)
        working_dir: Path = require_working_dir(state)

        verdict: DeterminationVerdict = resolve_verdict(state, working_dir, slug)
        sub_agent_spawned: Literal["execution", "notification"] | None = verdict.sub_agent_spawned

        report: DeterminationReport = DeterminationReport(
            slug=slug,
            determination=verdict.determination,
            reason=verdict.reason,
            failed_steps=list(verdict.failed_steps),
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
