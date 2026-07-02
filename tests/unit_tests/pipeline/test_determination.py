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
"""
from pathlib import Path
from typing import Callable

import pytest

from money_pit.constants import DETERMINATION_JSON_FILENAME as _REPORT_JSON_FILENAME
from money_pit.constants import EXECUTION_JOURNAL_FILENAME as _JOURNAL_FILENAME
from money_pit.graph.state import PipelineState
from money_pit.pipeline.determination import _FAILURE
from money_pit.pipeline.determination import _SUB_AGENT_EXECUTION
from money_pit.pipeline.determination import _SUB_AGENT_NOTIFICATION
from money_pit.pipeline.determination import _SUCCESS
from money_pit.pipeline.determination import DeterminationParseError
from money_pit.pipeline.determination import load_validation
from money_pit.pipeline.determination import make_determination_node
from money_pit.pipeline.determination import make_finalizer_node
from money_pit.pipeline.determination import map_execution_outcome
from money_pit.pipeline.determination import recompute_determination
from money_pit.schemas.determination import DeterminationReport
from money_pit.schemas.enums import Determination
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.validation_results import ActionStepsValidation
from money_pit.schemas.validation_results import ValidationStep


_SLUG = "2026-07-02_00-00-00"
_VALIDATION_FILENAME = "action_steps_validation.json"


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
        overall_status="validation_failed" if unmatched else "validated",
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
    validation = _make_validation(
        [_make_validation_step(f"A{i + 1:03d}", status) for i, status in enumerate(statuses)]
    )
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
    _ = (tmp_path / _VALIDATION_FILENAME).write_text(
        valid_validation.model_dump_json(indent=2), encoding="utf-8"
    )
    return tmp_path


def test_load_validation_with_valid_file(
    validation_working_dir: Path, valid_validation: ActionStepsValidation
) -> None:
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
    _ = (working_dir / _VALIDATION_FILENAME).write_text(
        validation.model_dump_json(indent=2), encoding="utf-8"
    )


def _write_journal(working_dir: Path, outcome: ExecutionOutcome) -> None:
    journal = ExecutionJournal(slug=_SLUG, outcome=outcome, entries=[])
    _ = (working_dir / _JOURNAL_FILENAME).write_text(
        journal.model_dump_json(indent=2), encoding="utf-8"
    )


def _read_report(working_dir: Path) -> DeterminationReport:
    return DeterminationReport.model_validate_json(
        (working_dir / _REPORT_JSON_FILENAME).read_text(encoding="utf-8")
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
    result = determination_node({"working_dir": str(parse_failure_working_dir)})
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
    result = determination_node({"working_dir": str(parse_failure_working_dir)})
    assert result["sub_agent_spawned"] is None


def test_make_determination_node_with_unmatched_step_sets_validation_error(
    determination_node: Callable[[PipelineState], dict[str, object]],
    unmatched_working_dir: Path,
) -> None:
    result = determination_node({"working_dir": str(unmatched_working_dir)})
    assert result["terminal_state"] == TerminalState.VALIDATION_ERROR


def test_make_determination_node_with_unmatched_step_records_failed_step(
    determination_node: Callable[[PipelineState], dict[str, object]],
    unmatched_working_dir: Path,
    unmatched_step_id: str,
) -> None:
    result = determination_node({"working_dir": str(unmatched_working_dir)})
    assert unmatched_step_id in result["failed_steps"]  # type: ignore[operator]


def test_make_determination_node_with_all_matched_leaves_terminal_state_unset(
    determination_node: Callable[[PipelineState], dict[str, object]],
    validation_working_dir: Path,
) -> None:
    result = determination_node({"working_dir": str(validation_working_dir)})
    assert result.get("terminal_state") is None


def _finalizer_state(
    working_dir: Path, determination: Determination, sub_agent_spawned: str | None
) -> PipelineState:
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
    _ = finalizer_node(
        _finalizer_state(clean_journal_working_dir, Determination.PROCEED, _SUB_AGENT_EXECUTION)
    )
    assert _read_report(clean_journal_working_dir).sub_agent_outcome == _SUCCESS


def test_make_finalizer_node_with_notification_reports_success(
    finalizer_node: Callable[[PipelineState], dict[str, object]],
    tmp_path: Path,
) -> None:
    _ = finalizer_node(_finalizer_state(tmp_path, Determination.HALT, _SUB_AGENT_NOTIFICATION))
    assert _read_report(tmp_path).sub_agent_outcome == _SUCCESS
