"""Tests for money_pit.pipeline.determination — the pure determination seam (wave S5 / ADR 0006).

Pins the three directly-testable pure functions the finalizer/determination step compose:
recompute_determination (all-MATCHED PROCEED / any-UNMATCHED HALT / empty-steps parse error),
load_validation (persisted file -> model, fail-closed on missing/malformed/schema-invalid),
and the fail-closed map_execution_outcome (None -> failure — an incomplete journal is never a
success). Also pins the two graph node callables directly (real DI, a tmp_path working dir and
hand-authored artifacts, no mocks): the determination node's fail-closed routing
(parse-failure -> ORCHESTRATION_ERROR with no sub-agent, any-UNMATCHED -> VALIDATION_ERROR,
all-MATCHED -> no halting terminal_state) and the finalizer node's fail-closed sub-agent outcome
(execution branch with no journal -> failure, clean journal -> success, notification -> success).
Assertions target Determination / TerminalState / literal return values and exception TYPES,
never message text.

Also pins the determination_verdict.json handoff that makes the finalizer stage-runnable: the
determination node writes the verdict — stamped with the run's slug — on all three return paths
(orchestration failure included), load_verdict distinguishes absent (None, a legitimate state) from
malformed (DeterminationVerdictError), resolve_verdict prefers disk over state and fails closed when
neither source carries a verdict, and a finalizer invoked with a minimal slug+working_dir state over
a populated run directory produces the same DeterminationReport as the state-driven path modulo
timestamp.

The verdict's slug is what makes a foreign run directory detectable, so its guard is pinned on both
halves: an artifact naming another run raises DeterminationVerdictError, and it does so even when
state carries a perfectly usable verdict — falling back there would conclude a foreign directory
into determination.json and look correct doing it.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from money_pit.constants import DETERMINATION_JSON_FILENAME as _REPORT_JSON_FILENAME
from money_pit.constants import DETERMINATION_VERDICT_JSON_FILENAME as _VERDICT_FILENAME
from money_pit.constants import EXECUTION_JOURNAL_FILENAME as _JOURNAL_FILENAME
from money_pit.graph.state import PipelineState
from money_pit.pipeline.determination import _FAILURE
from money_pit.pipeline.determination import _PARSE_FAILURE_REASON
from money_pit.pipeline.determination import _PROCEED_REASON
from money_pit.pipeline.determination import _SUB_AGENT_EXECUTION
from money_pit.pipeline.determination import _SUB_AGENT_NOTIFICATION
from money_pit.pipeline.determination import _SUCCESS
from money_pit.pipeline.determination import DeterminationParseError
from money_pit.pipeline.determination import DeterminationVerdictError
from money_pit.pipeline.determination import _halt_reason
from money_pit.pipeline.determination import _read_journal_outcome
from money_pit.pipeline.determination import _verdict_from_state
from money_pit.pipeline.determination import load_validation
from money_pit.pipeline.determination import load_verdict
from money_pit.pipeline.determination import make_determination_node
from money_pit.pipeline.determination import make_finalizer_node
from money_pit.pipeline.determination import map_execution_outcome
from money_pit.pipeline.determination import recompute_determination
from money_pit.pipeline.determination import resolve_verdict
from money_pit.schemas.determination import DeterminationReport
from money_pit.schemas.determination import DeterminationVerdict
from money_pit.schemas.enums import Determination
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import OverallValidationStatus
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.validation_results import ActionStepsValidation
from money_pit.schemas.validation_results import ValidationStep
from tests.unit_tests.conftest import CapturedLog


_SLUG = "2026-07-02_00-00-00"
_FOREIGN_SLUG = "2026-06-30_00-00-00"
_VALIDATION_FILENAME = "action_steps_validation.json"


def _minimal_state(working_dir: Path) -> PipelineState:
    return {"slug": _SLUG, "working_dir": str(working_dir)}


def _make_validation_step(step_id: str, status: ValidationStatus) -> ValidationStep:
    return ValidationStep(
        step_id=step_id,
        status=status,
        tool_sequence=None,
        compensation_sequence=None,
        gap_description=None if status == ValidationStatus.MATCHED else "gap",
    )


def _make_validation(steps: list[ValidationStep]) -> ActionStepsValidation:
    unmatched = any(step.status == ValidationStatus.UNMATCHED for step in steps)
    return ActionStepsValidation(
        slug=_SLUG,
        overall_status=(
            OverallValidationStatus.VALIDATION_FAILED if unmatched else OverallValidationStatus.VALIDATED
        ),
        steps=steps,
    )


def test_recompute_determination_with_all_matched() -> None:
    validation = _make_validation(
        [
            _make_validation_step("A001", ValidationStatus.MATCHED),
            _make_validation_step("A002", ValidationStatus.MATCHED),
        ]
    )
    assert recompute_determination(validation) == Determination.PROCEED


@pytest.mark.parametrize(
    "statuses",
    [
        [ValidationStatus.UNMATCHED],
        [ValidationStatus.MATCHED, ValidationStatus.UNMATCHED],
        [ValidationStatus.UNMATCHED, ValidationStatus.MATCHED],
    ],
)
def test_recompute_determination_with_any_unmatched(statuses: list[ValidationStatus]) -> None:
    validation = _make_validation([_make_validation_step(f"A{i + 1:03d}", status) for i, status in enumerate(statuses)])
    assert recompute_determination(validation) == Determination.HALT


def test_recompute_determination_with_empty_steps() -> None:
    validation = _make_validation([])
    with pytest.raises(DeterminationParseError):
        _ = recompute_determination(validation)


@pytest.fixture
def valid_validation() -> ActionStepsValidation:
    return _make_validation([_make_validation_step("A001", ValidationStatus.MATCHED)])


@pytest.fixture
def validation_working_dir(tmp_path: Path, valid_validation: ActionStepsValidation) -> Path:
    _ = (tmp_path / _VALIDATION_FILENAME).write_text(valid_validation.model_dump_json(indent=2), encoding="utf-8")
    return tmp_path


def test_load_validation_with_valid_file(validation_working_dir: Path, valid_validation: ActionStepsValidation) -> None:
    assert load_validation(validation_working_dir) == valid_validation


def test_load_validation_with_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DeterminationParseError):
        _ = load_validation(tmp_path)


def test_load_validation_with_malformed_json(tmp_path: Path) -> None:
    _ = (tmp_path / _VALIDATION_FILENAME).write_text("{ bad", encoding="utf-8")
    with pytest.raises(DeterminationParseError):
        _ = load_validation(tmp_path)


def test_load_validation_with_schema_invalid_json(tmp_path: Path) -> None:
    _ = (tmp_path / _VALIDATION_FILENAME).write_text('{"unexpected": "shape"}', encoding="utf-8")
    with pytest.raises(DeterminationParseError):
        _ = load_validation(tmp_path)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ExecutionOutcome.EXECUTED_CLEAN, "success"),
        (ExecutionOutcome.PARTIAL_COMPENSATED, "success"),
        (ExecutionOutcome.COMPENSATION_FAILED, "failure"),
        (ExecutionOutcome.EXECUTION_FAILED, "failure"),
        (None, "failure"),
    ],
)
def test_map_execution_outcome(outcome: ExecutionOutcome | None, expected: str) -> None:
    assert map_execution_outcome(outcome) == expected


def _write_validation(working_dir: Path, validation: ActionStepsValidation) -> None:
    _ = (working_dir / _VALIDATION_FILENAME).write_text(validation.model_dump_json(indent=2), encoding="utf-8")


def _write_journal(working_dir: Path, outcome: ExecutionOutcome) -> None:
    journal = ExecutionJournal(slug=_SLUG, outcome=outcome, entries=[])
    _ = (working_dir / _JOURNAL_FILENAME).write_text(journal.model_dump_json(indent=2), encoding="utf-8")


def _read_report(working_dir: Path) -> DeterminationReport:
    return DeterminationReport.model_validate_json((working_dir / _REPORT_JSON_FILENAME).read_text(encoding="utf-8"))


_JOURNAL_CORRUPT_LOG_FRAGMENT = "corrupt"


def test__read_journal_outcome_with_missing_journal_returns_none(tmp_path: Path) -> None:
    assert _read_journal_outcome(tmp_path) is None


def test__read_journal_outcome_with_valid_journal_returns_outcome(tmp_path: Path) -> None:
    _write_journal(tmp_path, ExecutionOutcome.EXECUTED_CLEAN)
    assert _read_journal_outcome(tmp_path) == ExecutionOutcome.EXECUTED_CLEAN


def test__read_journal_outcome_with_corrupt_journal_returns_none(tmp_path: Path) -> None:
    _ = (tmp_path / _JOURNAL_FILENAME).write_text('{"unexpected": "shape"}', encoding="utf-8")
    assert _read_journal_outcome(tmp_path) is None


def test__read_journal_outcome_with_corrupt_journal_logs_loudly(
    tmp_path: Path, loguru_records: list[CapturedLog]
) -> None:
    _ = (tmp_path / _JOURNAL_FILENAME).write_text('{"unexpected": "shape"}', encoding="utf-8")
    _ = _read_journal_outcome(tmp_path)
    assert any(
        record.level == "ERROR" and _JOURNAL_CORRUPT_LOG_FRAGMENT in record.message for record in loguru_records
    )


@pytest.fixture
def determination_node() -> Callable[[PipelineState], dict[str, object]]:
    return make_determination_node()


@pytest.fixture
def finalizer_node() -> Callable[[PipelineState], dict[str, object]]:
    return make_finalizer_node()


@pytest.fixture
def parse_failure_working_dir(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    content: str | None = getattr(request, "param", None)
    if content is not None:
        _ = (tmp_path / _VALIDATION_FILENAME).write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture
def unmatched_step_id() -> str:
    return "A007"


@pytest.fixture
def unmatched_working_dir(tmp_path: Path, unmatched_step_id: str) -> Path:
    _write_validation(
        tmp_path,
        _make_validation(
            [
                _make_validation_step("A001", ValidationStatus.MATCHED),
                _make_validation_step(unmatched_step_id, ValidationStatus.UNMATCHED),
            ]
        ),
    )
    return tmp_path


@pytest.mark.parametrize(
    "parse_failure_working_dir",
    [None, "{ bad", '{"unexpected": "shape"}'],
    indirect=True,
)
def test_make_determination_node_with_parse_failure_sets_orchestration_error(
    determination_node: Callable[[PipelineState], dict[str, object]],
    parse_failure_working_dir: Path,
) -> None:
    result = determination_node(_minimal_state(parse_failure_working_dir))
    assert result["terminal_state"] == TerminalState.ORCHESTRATION_ERROR


@pytest.mark.parametrize(
    "parse_failure_working_dir",
    [None, "{ bad", '{"unexpected": "shape"}'],
    indirect=True,
)
def test_make_determination_node_with_parse_failure_spawns_no_sub_agent(
    determination_node: Callable[[PipelineState], dict[str, object]],
    parse_failure_working_dir: Path,
) -> None:
    result = determination_node(_minimal_state(parse_failure_working_dir))
    assert result["sub_agent_spawned"] is None


def test_make_determination_node_with_unmatched_step_sets_validation_error(
    determination_node: Callable[[PipelineState], dict[str, object]],
    unmatched_working_dir: Path,
) -> None:
    result = determination_node(_minimal_state(unmatched_working_dir))
    assert result["terminal_state"] == TerminalState.VALIDATION_ERROR


def test_make_determination_node_with_unmatched_step_records_failed_step(
    determination_node: Callable[[PipelineState], dict[str, object]],
    unmatched_working_dir: Path,
    unmatched_step_id: str,
) -> None:
    result = determination_node(_minimal_state(unmatched_working_dir))
    assert unmatched_step_id in result["failed_steps"]  # type: ignore[operator]


def test_make_determination_node_with_all_matched_leaves_terminal_state_unset(
    determination_node: Callable[[PipelineState], dict[str, object]],
    validation_working_dir: Path,
) -> None:
    result = determination_node(_minimal_state(validation_working_dir))
    assert result.get("terminal_state") is None


def _write_verdict_file(working_dir: Path, verdict: DeterminationVerdict) -> None:
    _ = (working_dir / _VERDICT_FILENAME).write_text(verdict.model_dump_json(indent=2), encoding="utf-8")


def _read_verdict_file(working_dir: Path) -> DeterminationVerdict:
    return DeterminationVerdict.model_validate_json((working_dir / _VERDICT_FILENAME).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "parse_failure_working_dir",
    [None, "{ bad", '{"unexpected": "shape"}'],
    indirect=True,
)
def test_make_determination_node_with_parse_failure_writes_verdict(
    determination_node: Callable[[PipelineState], dict[str, object]],
    parse_failure_working_dir: Path,
) -> None:
    """A malformed input must still leave a truthful artifact — a stage runner has nothing else to read."""
    _ = determination_node(_minimal_state(parse_failure_working_dir))
    assert _read_verdict_file(parse_failure_working_dir) == DeterminationVerdict(
        slug=_SLUG,
        determination=Determination.HALT,
        reason=_PARSE_FAILURE_REASON,
        failed_steps=[],
        sub_agent_spawned=None,
    )


def test_make_determination_node_with_unmatched_step_writes_verdict(
    determination_node: Callable[[PipelineState], dict[str, object]],
    unmatched_working_dir: Path,
    unmatched_step_id: str,
) -> None:
    _ = determination_node(_minimal_state(unmatched_working_dir))
    assert _read_verdict_file(unmatched_working_dir) == DeterminationVerdict(
        slug=_SLUG,
        determination=Determination.HALT,
        reason=_halt_reason([unmatched_step_id]),
        failed_steps=[unmatched_step_id],
        sub_agent_spawned=_SUB_AGENT_NOTIFICATION,
    )


def test_make_determination_node_with_all_matched_writes_verdict(
    determination_node: Callable[[PipelineState], dict[str, object]],
    validation_working_dir: Path,
) -> None:
    _ = determination_node(_minimal_state(validation_working_dir))
    assert _read_verdict_file(validation_working_dir) == DeterminationVerdict(
        slug=_SLUG,
        determination=Determination.PROCEED,
        reason=_PROCEED_REASON,
        failed_steps=[],
        sub_agent_spawned=_SUB_AGENT_EXECUTION,
    )


@pytest.fixture
def persisted_verdict(request: pytest.FixtureRequest, persisted_verdict__slug: str) -> DeterminationVerdict:
    return getattr(
        request,
        "param",
        DeterminationVerdict(
            slug=persisted_verdict__slug,
            determination=Determination.PROCEED,
            reason=_PROCEED_REASON,
            failed_steps=[],
            sub_agent_spawned=_SUB_AGENT_EXECUTION,
        ),
    )


@pytest.fixture
def persisted_verdict__slug(request: pytest.FixtureRequest) -> str:
    return getattr(request, "param", _SLUG)


@pytest.fixture
def state_verdict(unmatched_step_id: str) -> DeterminationVerdict:
    """Deliberately distinguishable from persisted_verdict on every field but slug, so precedence is observable."""
    return DeterminationVerdict(
        slug=_SLUG,
        determination=Determination.HALT,
        reason=_halt_reason([unmatched_step_id]),
        failed_steps=[unmatched_step_id],
        sub_agent_spawned=_SUB_AGENT_NOTIFICATION,
    )


@pytest.fixture
def verdict_working_dir(tmp_path: Path, persisted_verdict: DeterminationVerdict) -> Path:
    _write_verdict_file(tmp_path, persisted_verdict)
    return tmp_path


def test_load_verdict_with_absent_artifact(tmp_path: Path) -> None:
    """Absent is a legitimate state — the gate simply has not run yet."""
    assert load_verdict(tmp_path) is None


def test_load_verdict_with_valid_artifact(verdict_working_dir: Path, persisted_verdict: DeterminationVerdict) -> None:
    assert load_verdict(verdict_working_dir) == persisted_verdict


@pytest.mark.parametrize("content", ["{ bad", '{"unexpected": "shape"}'])
def test_load_verdict_with_malformed_artifact(tmp_path: Path, content: str) -> None:
    _ = (tmp_path / _VERDICT_FILENAME).write_text(content, encoding="utf-8")
    with pytest.raises(DeterminationVerdictError):
        _ = load_verdict(tmp_path)


def _state_from_verdict(working_dir: Path, verdict: DeterminationVerdict) -> PipelineState:
    return {
        "slug": _SLUG,
        "working_dir": str(working_dir),
        "determination": verdict.determination,
        "sub_agent_spawned": verdict.sub_agent_spawned,
        "determination_reason": verdict.reason,
        "failed_steps": list(verdict.failed_steps),
    }


def test__verdict_from_state_with_determination(tmp_path: Path, state_verdict: DeterminationVerdict) -> None:
    assert _verdict_from_state(_state_from_verdict(tmp_path, state_verdict), _SLUG) == state_verdict


def test__verdict_from_state_stamps_the_runs_slug(tmp_path: Path, state_verdict: DeterminationVerdict) -> None:
    """The slug comes from the run, not the state dict, so a state-derived verdict is attributable too."""
    assert _verdict_from_state(_state_from_verdict(tmp_path, state_verdict), _FOREIGN_SLUG).slug == _FOREIGN_SLUG


def test__verdict_from_state_without_determination(tmp_path: Path) -> None:
    with pytest.raises(DeterminationVerdictError):
        _ = _verdict_from_state(_minimal_state(tmp_path), _SLUG)


def test_resolve_verdict_with_artifact_and_state_prefers_artifact(
    verdict_working_dir: Path,
    persisted_verdict: DeterminationVerdict,
    state_verdict: DeterminationVerdict,
) -> None:
    resolved = resolve_verdict(_state_from_verdict(verdict_working_dir, state_verdict), verdict_working_dir, _SLUG)
    assert resolved == persisted_verdict


def test_resolve_verdict_with_absent_artifact_falls_back_to_state(
    tmp_path: Path, state_verdict: DeterminationVerdict
) -> None:
    assert resolve_verdict(_state_from_verdict(tmp_path, state_verdict), tmp_path, _SLUG) == state_verdict


def test_resolve_verdict_with_neither_source(tmp_path: Path) -> None:
    with pytest.raises(DeterminationVerdictError):
        _ = resolve_verdict(_minimal_state(tmp_path), tmp_path, _SLUG)


def test_resolve_verdict_with_malformed_artifact_does_not_fall_back_to_state(
    tmp_path: Path, state_verdict: DeterminationVerdict
) -> None:
    """A corrupt artifact is not an absent one; silently preferring state would conceal a broken run directory."""
    _ = (tmp_path / _VERDICT_FILENAME).write_text("{ bad", encoding="utf-8")
    with pytest.raises(DeterminationVerdictError):
        _ = resolve_verdict(_state_from_verdict(tmp_path, state_verdict), tmp_path, _SLUG)


@pytest.mark.parametrize("persisted_verdict__slug", [_FOREIGN_SLUG], indirect=True)
def test_resolve_verdict_with_foreign_slug_artifact_does_not_fall_back_to_state(
    verdict_working_dir: Path, state_verdict: DeterminationVerdict
) -> None:
    """The staleness guard: the state here carries a usable verdict, and taking it would conceal a foreign run dir."""
    with pytest.raises(DeterminationVerdictError):
        _ = resolve_verdict(_state_from_verdict(verdict_working_dir, state_verdict), verdict_working_dir, _SLUG)


@pytest.mark.parametrize("persisted_verdict__slug", [_FOREIGN_SLUG], indirect=True)
def test_resolve_verdict_with_foreign_slug_artifact_names_both_runs(
    verdict_working_dir: Path, state_verdict: DeterminationVerdict
) -> None:
    """Naming both slugs is what makes the failure actionable — which directory, and which run it belongs to."""
    with pytest.raises(DeterminationVerdictError) as error:
        _ = resolve_verdict(_state_from_verdict(verdict_working_dir, state_verdict), verdict_working_dir, _SLUG)
    message = str(error.value)
    assert _FOREIGN_SLUG in message
    assert _SLUG in message


def _finalizer_state(working_dir: Path, determination: Determination, sub_agent_spawned: str | None) -> PipelineState:
    return {
        "slug": _SLUG,
        "working_dir": str(working_dir),
        "determination": determination,
        "sub_agent_spawned": sub_agent_spawned,
        "determination_reason": "reason",
        "failed_steps": [],
    }


@pytest.fixture
def clean_journal_working_dir(tmp_path: Path) -> Path:
    _write_journal(tmp_path, ExecutionOutcome.EXECUTED_CLEAN)
    return tmp_path


def test_make_finalizer_node_with_execution_and_missing_journal_reports_failure(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    tmp_path: Path,
) -> None:
    _ = finalizer_node(_finalizer_state(tmp_path, Determination.PROCEED, _SUB_AGENT_EXECUTION))
    assert _read_report(tmp_path).sub_agent_outcome == _FAILURE


def test_make_finalizer_node_with_execution_and_clean_journal_reports_success(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    clean_journal_working_dir: Path,
) -> None:
    _ = finalizer_node(_finalizer_state(clean_journal_working_dir, Determination.PROCEED, _SUB_AGENT_EXECUTION))
    assert _read_report(clean_journal_working_dir).sub_agent_outcome == _SUCCESS


def test_make_finalizer_node_with_notification_reports_success(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    tmp_path: Path,
) -> None:
    _ = finalizer_node(_finalizer_state(tmp_path, Determination.HALT, _SUB_AGENT_NOTIFICATION))
    assert _read_report(tmp_path).sub_agent_outcome == _SUCCESS


def _verdict_of(report: DeterminationReport) -> DeterminationVerdict:
    return DeterminationVerdict(
        slug=report.slug,
        determination=report.determination,
        reason=report.reason,
        failed_steps=list(report.failed_steps),
        sub_agent_spawned=report.sub_agent_spawned,
    )


@pytest.fixture
def standalone_run_dir(tmp_path: Path, persisted_verdict: DeterminationVerdict) -> Path:
    """A run directory a stage runner could arrive at cold: the gate's verdict plus the sub-agent's journal."""
    run_dir = tmp_path / "standalone"
    run_dir.mkdir()
    _write_verdict_file(run_dir, persisted_verdict)
    _write_journal(run_dir, ExecutionOutcome.EXECUTED_CLEAN)
    return run_dir


@pytest.fixture
def state_driven_run_dir(tmp_path: Path) -> Path:
    """The same run directory minus the verdict artifact, so the finalizer must take the state path."""
    run_dir = tmp_path / "state_driven"
    run_dir.mkdir()
    _write_journal(run_dir, ExecutionOutcome.EXECUTED_CLEAN)
    return run_dir


def test_make_finalizer_node_with_minimal_state_matches_state_driven_report(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    standalone_run_dir: Path,
    state_driven_run_dir: Path,
    persisted_verdict: DeterminationVerdict,
) -> None:
    """The stage-runnability pin: slug + working_dir alone reproduce the state-driven report, modulo timestamp."""
    _ = finalizer_node(_minimal_state(standalone_run_dir))
    _ = finalizer_node(_state_from_verdict(state_driven_run_dir, persisted_verdict))
    assert _read_report(standalone_run_dir).model_dump(exclude={"timestamp"}) == _read_report(
        state_driven_run_dir
    ).model_dump(exclude={"timestamp"})


def test_make_finalizer_node_with_minimal_state_resolves_sub_agent_outcome(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    standalone_run_dir: Path,
) -> None:
    """The outcome is re-derived from the journal on disk, not carried in from the graph run."""
    _ = finalizer_node(_minimal_state(standalone_run_dir))
    assert _read_report(standalone_run_dir).sub_agent_outcome == _SUCCESS


def test_make_finalizer_node_with_state_disagreeing_on_determination_reports_persisted_verdict(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    standalone_run_dir: Path,
    state_verdict: DeterminationVerdict,
) -> None:
    """Same run, different go/no-go: disk wins end to end, because a re-run of determination updates the artifact."""
    _ = finalizer_node(_state_from_verdict(standalone_run_dir, state_verdict))
    assert _verdict_of(_read_report(standalone_run_dir)) == DeterminationVerdict(
        slug=_SLUG,
        determination=Determination.PROCEED,
        reason=_PROCEED_REASON,
        failed_steps=[],
        sub_agent_spawned=_SUB_AGENT_EXECUTION,
    )


@pytest.mark.parametrize("persisted_verdict__slug", [_FOREIGN_SLUG], indirect=True)
def test_make_finalizer_node_with_artifact_from_another_run_raises(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    standalone_run_dir: Path,
    state_verdict: DeterminationVerdict,
) -> None:
    """Different run, whatever go/no-go: neither source is trustworthy, so the run cannot be concluded."""
    with pytest.raises(DeterminationVerdictError):
        _ = finalizer_node(_state_from_verdict(standalone_run_dir, state_verdict))


@pytest.mark.parametrize("persisted_verdict__slug", [_FOREIGN_SLUG], indirect=True)
def test_make_finalizer_node_with_artifact_from_another_run_writes_no_report(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    standalone_run_dir: Path,
    state_verdict: DeterminationVerdict,
) -> None:
    """The consequence the guard exists to prevent: no determination.json records a foreign go/no-go."""
    with pytest.raises(DeterminationVerdictError):
        _ = finalizer_node(_state_from_verdict(standalone_run_dir, state_verdict))
    assert not (standalone_run_dir / _REPORT_JSON_FILENAME).exists()


def test_make_finalizer_node_with_minimal_state_and_no_verdict(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    state_driven_run_dir: Path,
) -> None:
    """A run directory whose gate never decided cannot be concluded — fail closed rather than invent a verdict."""
    with pytest.raises(DeterminationVerdictError):
        _ = finalizer_node(_minimal_state(state_driven_run_dir))
