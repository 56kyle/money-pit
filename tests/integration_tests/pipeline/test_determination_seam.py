"""Integration seam: persisted action_steps_validation.json -> determination decision (wave S5 / ADR 0006).

Pins the validation-error branch of the determination step — the composition of load_validation
and recompute_determination over a real on-disk UNMATCHED validation, yielding HALT and the
non-empty failed-step set the finalizer records as failed_steps.

The full observable validation-error path (determination.json with sub_agent_spawned="notification"
plus a send_email call) cannot be driven end-to-end through run_pipeline today: client_order_id is
derived deterministically as f"{slug}:{step_id}" (execution_params.build_execution_params) and the
validator recomputes the same expected value, while a schema-violating payload raises inside
build_execution_params before validation runs — so PipelineOverrides exposes no injection point that
produces an UNMATCHED step. Pinning that observable half is deferred until either PipelineOverrides
gains a manifest/validation seam or the finalizer node's signature is fixed.
"""

from pathlib import Path

from money_pit.pipeline.determination import load_validation, recompute_determination
from money_pit.schemas.enums import Determination, ValidationStatus
from money_pit.schemas.validation_results import ActionStepsValidation, ValidationStep

_SLUG = "2026-07-02_00-00-00"
_VALIDATION_FILENAME = "action_steps_validation.json"


def _write_validation_error(working_dir: Path) -> None:
    validation = ActionStepsValidation(
        slug=_SLUG,
        overall_status="validation_failed",
        steps=[
            ValidationStep(
                step_id="A001",
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
