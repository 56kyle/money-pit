"""Module containing immutable generic run manifests for the money_pit package."""

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from money_pit.constants import RUN_MANIFEST_FILENAME
from money_pit.runs.errors import RunAlreadyExistsError
from money_pit.runs.errors import RunManifestWriteError
from money_pit.runs.paths import RepositoryPaths


_MANIFEST_SCHEMA_VERSION: Final[int] = 1


@dataclass(frozen=True)
class RunManifest:
    """An immutable UUID4 run identity with an explicit creation instant."""

    run_id: uuid.UUID
    created_at: datetime
    schema_version: int = _MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate identity, time, and manifest schema invariants."""
        if self.run_id.version != 4:
            raise ValueError("run_id must be a UUID4 value.")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware.")
        if self.schema_version != _MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {_MANIFEST_SCHEMA_VERSION}.")

    def to_json(self) -> str:
        """Serialize the manifest using stable field order and UTC timestamps."""
        created_at_utc: datetime = self.created_at.astimezone(timezone.utc)
        return (
            json.dumps(
                {
                    "schema_version": self.schema_version,
                    "run_id": str(self.run_id),
                    "created_at": created_at_utc.isoformat(),
                },
                indent=2,
            )
            + "\n"
        )


def create_run(
    paths: RepositoryPaths,
    *,
    created_at: datetime | None = None,
    run_id: uuid.UUID | None = None,
) -> tuple[RunManifest, Path]:
    """Create one UUID4 run directory and its manifest atomically."""
    manifest: RunManifest = RunManifest(
        run_id=run_id if run_id is not None else uuid.uuid4(),
        created_at=created_at if created_at is not None else datetime.now(tz=timezone.utc),
    )
    run_dir: Path = paths.runs_root / str(manifest.run_id)
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise RunAlreadyExistsError(f"Run directory already exists for {manifest.run_id}.") from error
    manifest_path: Path = run_dir / RUN_MANIFEST_FILENAME
    temporary_path: Path | None = None
    try:
        file_descriptor: int
        temporary_name: str
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=run_dir,
            prefix=".run.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as manifest_file:
            manifest_file.write(manifest.to_json())
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        temporary_path.replace(manifest_path)
        temporary_path = None
    except OSError as error:
        raise RunManifestWriteError(f"Could not write manifest for run {manifest.run_id}.") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return manifest, run_dir
