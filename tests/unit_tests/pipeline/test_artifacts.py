from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from money_pit.pipeline.artifacts import install_stage_artifact_file
from money_pit.pipeline.artifacts import reconcile_stage_artifact_files
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import StageArtifactRecord
from money_pit.schemas.runs import bind_artifact_record


def test_reconcile_stage_artifact_files_accepts_equal_payload_with_reordered_keys(tmp_path: Path) -> None:
    recorded_at = datetime(2026, 8, 9, tzinfo=UTC)
    artifact = StageArtifactRecord.from_payload(
        run_id="4fa85f64-5717-4562-b3fc-2c963f66afa6",
        stage="A1",
        requested_as_of=recorded_at,
        started_at=recorded_at,
        decision_at=recorded_at,
        known_at=recorded_at,
        input_ids=(bind_artifact_record(ArtifactRecordKind.ASSET, "input-1"),),
        output_ids=(bind_artifact_record(ArtifactRecordKind.OBSERVATION, "output-1"),),
        implementation_version="artifact-test",
        payload={"zeta": {"second": 2, "first": 1}, "alpha": "value"},
    )
    run_dir = tmp_path / "run"
    install_stage_artifact_file(run_dir, artifact)
    installed = (run_dir / "a1.json").read_bytes()
    reordered = artifact.model_copy(update={"payload": {"alpha": "value", "zeta": {"first": 1, "second": 2}}})

    reconcile_stage_artifact_files(run_dir, (reordered,))

    assert (run_dir / "a1.json").read_bytes() == installed


def test_stage_artifact_rejects_unqualified_durable_record_bindings() -> None:
    recorded_at = datetime(2026, 8, 9, tzinfo=UTC)

    with pytest.raises(ValidationError, match="namespace"):
        _ = StageArtifactRecord.from_payload(
            run_id="4fa85f64-5717-4562-b3fc-2c963f66afa6",
            stage="A1",
            requested_as_of=recorded_at,
            started_at=recorded_at,
            decision_at=recorded_at,
            known_at=recorded_at,
            input_ids=("unqualified-input",),
            output_ids=(),
            implementation_version="artifact-test",
            payload={},
        )
