"""Module writing immutable, content-identified run stage artifacts."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from money_pit.pipeline.chain import Stage
from money_pit.schemas.runs import StageArtifactRecord


class StageArtifactCollisionError(Exception):
    """Raised when a stage path is already bound to different content."""


class StageArtifactInstallationIncompleteError(Exception):
    """Raised when the DB-authoritative artifact still needs file installation."""

    def __init__(self, artifact: StageArtifactRecord, path: Path) -> None:
        """Retain exact recovery inputs without rerunning a stage."""
        self.artifact: StageArtifactRecord = artifact
        self.path: Path = path
        super().__init__(f"Stage artifact file installation is incomplete: {path}")


StageArtifact = StageArtifactRecord


class StageArtifactStore(Protocol):
    """Append-only durable authority for stage artifact records."""

    def append_stage_artifact(self, artifact: StageArtifactRecord) -> None:
        """Persist one immutable artifact and its exact run delta bindings."""
        ...


def persist_stage_artifact(
    run_dir: Path,
    *,
    run_id: str,
    stage: Stage,
    requested_as_of: datetime,
    started_at: datetime,
    known_at: datetime,
    decision_at: datetime,
    input_ids: tuple[str, ...],
    output_ids: tuple[str, ...],
    implementation_version: str,
    payload: BaseModel,
    artifact_store: StageArtifactStore,
) -> StageArtifact:
    """Write one stage artifact atomically and idempotently."""
    for value in (requested_as_of, started_at, known_at, decision_at):
        _require_aware(value)
    artifact = build_stage_artifact(
        run_id=run_id,
        stage=stage,
        requested_as_of=requested_as_of,
        started_at=started_at,
        known_at=known_at,
        decision_at=decision_at,
        input_ids=input_ids,
        output_ids=output_ids,
        implementation_version=implementation_version,
        payload=payload,
    )
    artifact_store.append_stage_artifact(artifact)
    _ = try_install_stage_artifact_file(run_dir, artifact)
    return artifact


def build_stage_artifact(
    *,
    run_id: str,
    stage: Stage,
    requested_as_of: datetime,
    started_at: datetime,
    known_at: datetime,
    decision_at: datetime,
    input_ids: tuple[str, ...],
    output_ids: tuple[str, ...],
    implementation_version: str,
    payload: BaseModel,
) -> StageArtifact:
    """Build one exact record for atomic repository admission."""
    for value in (requested_as_of, started_at, known_at, decision_at):
        _require_aware(value)
    return StageArtifactRecord.from_payload(
        run_id=run_id,
        stage=stage.value,
        requested_as_of=requested_as_of,
        started_at=started_at,
        known_at=known_at,
        decision_at=decision_at,
        input_ids=input_ids,
        output_ids=output_ids,
        implementation_version=implementation_version,
        payload=payload.model_dump(mode="json"),
    )


def install_stage_artifact_file(run_dir: Path, artifact: StageArtifactRecord) -> None:
    """Install or reconcile a DB-authoritative artifact file without model execution."""
    encoded: bytes = _encode_stage_artifact(artifact)
    path: Path = run_dir / f"{artifact.stage.casefold()}.json"
    if path.exists():
        if path.read_bytes() != encoded:
            raise StageArtifactCollisionError(f"Stage artifact already exists with different content: {path}")
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_create(path, encoded)
    except OSError as error:
        raise StageArtifactInstallationIncompleteError(artifact, path) from error


def try_install_stage_artifact_file(
    run_dir: Path,
    artifact: StageArtifactRecord,
) -> bool:
    """Attempt derived-file installation while retaining DB authority for recovery."""
    try:
        install_stage_artifact_file(run_dir, artifact)
    except StageArtifactInstallationIncompleteError:
        return False
    return True


def reconcile_stage_artifact_files(
    run_dir: Path,
    artifacts: tuple[StageArtifactRecord, ...],
) -> None:
    """Install every DB-authoritative file without rerunning model stages."""
    for artifact in artifacts:
        install_stage_artifact_file(run_dir, artifact)


def _encode_stage_artifact(artifact: StageArtifactRecord) -> bytes:
    """Serialize equal artifact records to identical file bytes."""
    serialized = json.dumps(
        artifact.model_dump(mode="json"),
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    return f"{serialized}\n".encode("utf-8")


def _atomic_create(path: Path, content: bytes) -> None:
    file_descriptor: int
    temporary_name: str
    file_descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    temporary_path: Path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            _ = stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as error:
            if path.read_bytes() != content:
                raise StageArtifactCollisionError(
                    f"Stage artifact already exists with different content: {path}",
                ) from error
    finally:
        temporary_path.unlink(missing_ok=True)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Stage artifact timestamps must be timezone-aware")
