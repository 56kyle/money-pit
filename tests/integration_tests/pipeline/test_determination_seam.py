"""Integration seam: persisted action_steps_validation.json -> determination decision (wave S5 / ADR 0006).

Pins the validation-error branch of the determination step — the composition of load_validation
and recompute_determination over a real on-disk UNMATCHED validation, yielding HALT and the
non-empty failed-step set the finalizer records as failed_steps.

Also pins the disk-only handoff between the two nodes: the determination node writes
determination_verdict.json, and a finalizer invoked afterwards with a state carrying nothing but
slug and working_dir reproduces the full DeterminationReport. Nothing but the run directory passes
between the two calls, which is what makes each node independently stage-runnable — and the same
seam pins its safety catch: a finalizer pointed at a directory whose verdict names another run
raises rather than concluding it, so a mis-pointed stage runner writes no determination.json.

The full observable validation-error path (determination.json with sub_agent_spawned="notification"
plus a send_email call) cannot be driven end-to-end through run_pipeline today: client_order_id is
derived deterministically as f"{slug}:{step_id}" (execution_params.build_execution_params) and the
validator recomputes the same expected value, while a schema-violating payload raises inside
build_execution_params before validation runs — so PipelineOverrides exposes no injection point that
produces an UNMATCHED step. Pinning that observable half is deferred until either PipelineOverrides
gains a manifest/validation seam or the finalizer node's signature is fixed.
"""

from pathlib import Path

import pytest

from money_pit.constants import DETERMINATION_JSON_FILENAME
from money_pit.constants import DETERMINATION_VERDICT_JSON_FILENAME
from money_pit.graph.state import PipelineState
from money_pit.pipeline.determination import DeterminationVerdictError
from money_pit.pipeline.determination import make_determination_node
from money_pit.pipeline.determination import make_finalizer_node
from money_pit.pipeline.determination import load_validation
from money_pit.pipeline.determination import recompute_determination
from money_pit.schemas.determination import DeterminationReport
from money_pit.schemas.determination import DeterminationVerdict
from money_pit.schemas.enums import Determination
from money_pit.schemas.enums import OverallValidationStatus
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.validation_results import ActionStepsValidation
from money_pit.schemas.validation_results import ValidationStep


_SLUG = "2026-07-02_00-00-00"
_FOREIGN_SLUG = "2026-06-30_00-00-00"
_VALIDATION_FILENAME = "action_steps_validation.json"
_UNMATCHED_STEP_ID = "A001"


def _write_validation_error(working_dir: Path) -> None:
    validation = ActionStepsValidation(
        slug=_SLUG,
        overall_status=OverallValidationStatus.VALIDATION_FAILED,
        steps=[
            ValidationStep(
                step_id=_UNMATCHED_STEP_ID,
                status=ValidationStatus.UNMATCHED,
                tool_sequence=None,
                compensation_sequence=None,
                gap_description="client_order_id mismatch",
            ),
        ],
    )
    _ = (working_dir / _VALIDATION_FILENAME).write_text(validation.model_dump_json(indent=2), encoding="utf-8")


def test_determination_seam_with_validation_error_halts(tmp_path: Path) -> None:
    _write_validation_error(tmp_path)
    validation = load_validation(tmp_path)
    assert recompute_determination(validation) == Determination.HALT


def test_determination_seam_with_validation_error_has_failed_steps(tmp_path: Path) -> None:
    _write_validation_error(tmp_path)
    validation = load_validation(tmp_path)
    failed_steps = [s.step_id for s in validation.steps if s.status == ValidationStatus.UNMATCHED]
    assert failed_steps


def _run_determination_then_finalizer(working_dir: Path) -> DeterminationReport:
    """Run both nodes over the run directory, passing nothing but slug and working_dir into the finalizer."""
    _ = make_determination_node()({"slug": _SLUG, "working_dir": str(working_dir)})
    _ = make_finalizer_node()({"slug": _SLUG, "working_dir": str(working_dir)})
    return DeterminationReport.model_validate_json(
        (working_dir / DETERMINATION_JSON_FILENAME).read_text(encoding="utf-8")
    )


@pytest.fixture
def validation_error_run_dir(tmp_path: Path) -> Path:
    _write_validation_error(tmp_path)
    return tmp_path


@pytest.fixture
def parse_failure_run_dir(tmp_path: Path) -> Path:
    _ = (tmp_path / _VALIDATION_FILENAME).write_text("{ bad", encoding="utf-8")
    return tmp_path


def test_determination_seam_with_validation_error_writes_verdict(validation_error_run_dir: Path) -> None:
    _ = make_determination_node()({"slug": _SLUG, "working_dir": str(validation_error_run_dir)})
    persisted = DeterminationVerdict.model_validate_json(
        (validation_error_run_dir / DETERMINATION_VERDICT_JSON_FILENAME).read_text(encoding="utf-8")
    )
    assert (persisted.determination, persisted.failed_steps) == (Determination.HALT, [_UNMATCHED_STEP_ID])


def test_determination_seam_with_validation_error_reaches_finalizer_through_disk(
    validation_error_run_dir: Path,
) -> None:
    report = _run_determination_then_finalizer(validation_error_run_dir)
    assert (report.determination, report.sub_agent_spawned, report.failed_steps) == (
        Determination.HALT,
        "notification",
        [_UNMATCHED_STEP_ID],
    )


def test_determination_seam_with_parse_failure_reaches_finalizer_through_disk(parse_failure_run_dir: Path) -> None:
    """The path most likely to strand a stage runner: a malformed input still concludes into a HALT record."""
    report = _run_determination_then_finalizer(parse_failure_run_dir)
    assert (report.determination, report.sub_agent_spawned) == (Determination.HALT, None)


@pytest.fixture
def decided_run_dir(validation_error_run_dir: Path) -> Path:
    """A run directory the determination node has genuinely decided, verdict artifact and all."""
    _ = make_determination_node()({"slug": _SLUG, "working_dir": str(validation_error_run_dir)})
    return validation_error_run_dir


def test_determination_seam_with_finalizer_on_another_runs_directory_raises(decided_run_dir: Path) -> None:
    """The mis-pointed stage runner: the verdict on disk was written for a different run, so nothing is concluded."""
    with pytest.raises(DeterminationVerdictError):
        _ = make_finalizer_node()({"slug": _FOREIGN_SLUG, "working_dir": str(decided_run_dir)})


def test_determination_seam_with_finalizer_on_another_runs_directory_writes_no_report(
    decided_run_dir: Path,
) -> None:
    with pytest.raises(DeterminationVerdictError):
        _ = make_finalizer_node()({"slug": _FOREIGN_SLUG, "working_dir": str(decided_run_dir)})
    assert not (decided_run_dir / DETERMINATION_JSON_FILENAME).exists()
