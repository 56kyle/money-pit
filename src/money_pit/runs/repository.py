"""Module resolving complete immutable run records."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from money_pit.constants import RUN_MANIFEST_FILENAME
from money_pit.runs.paths import RepositoryPaths
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import validate_run_id


class RunNotFoundError(Exception):
    """Raised when a run manifest is absent."""


class RunManifestReadError(Exception):
    """Raised when a run manifest is malformed or has the wrong identity."""


@dataclass(frozen=True)
class ResolvedRun:
    """One complete immutable run and its artifact directory."""

    record: RunRecord
    path: Path

    @property
    def run_id(self) -> str:
        """Return the durable run identifier."""
        return self.record.run_id


class RunRepository:
    """Resolve complete filesystem run records without compatibility fallbacks."""

    def __init__(self, paths: RepositoryPaths) -> None:
        """Bind the dedicated run artifact root."""
        self._paths: RepositoryPaths = paths

    def resolve(self, run_id: str) -> ResolvedRun:
        """Resolve one complete immutable run record."""
        try:
            _ = validate_run_id(run_id)
        except ValueError as error:
            raise RunNotFoundError("Run ID must be a canonical UUID4 string.") from error
        path = self._paths.runs_root / run_id
        manifest_path = path / RUN_MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise RunNotFoundError(f"Run {run_id!r} is not present in the run repository.")
        try:
            record = RunRecord.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError) as error:
            raise RunManifestReadError(f"Run manifest is invalid: {manifest_path}.") from error
        if record.run_id != run_id:
            raise RunManifestReadError("Run manifest identity does not match its directory.")
        return ResolvedRun(record=record, path=path)
