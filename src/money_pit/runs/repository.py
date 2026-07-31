"""Module containing native and legacy run resolution for the money_pit package."""
# pyright: reportAny=false

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from money_pit.constants import RUN_MANIFEST_FILENAME
from money_pit.runs.manifest import RunManifest
from money_pit.runs.paths import RepositoryPaths
from money_pit.storage.database import Database


if TYPE_CHECKING:
    import sqlite3


class RunNotFoundError(Exception):
    """Raised when neither a native manifest nor legacy index contains a run."""


class RunManifestReadError(Exception):
    """Raised when a native run manifest cannot be validated."""


@dataclass(frozen=True)
class ResolvedRun:
    """One resolved immutable artifact snapshot."""

    run_id: str
    path: Path
    created_at: datetime | None
    origin: str


class RunRepository:
    """Resolve native manifests and read-only indexed legacy runs."""

    def __init__(self, paths: RepositoryPaths, database: Database) -> None:
        """Bind the repository to generic paths and an initialized database."""
        self._paths: RepositoryPaths = paths
        self._database: Database = database

    def resolve(self, run_id: str) -> ResolvedRun:
        """Resolve a native UUID4 or indexed legacy identifier."""
        native: ResolvedRun | None = self._resolve_native(run_id)
        if native is not None:
            return native
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = connection.execute(
                """
                SELECT legacy_run_id, legacy_path, inferred_created_at
                FROM legacy_runs
                WHERE legacy_run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise RunNotFoundError(f"Run {run_id!r} is not present in the run repository.")
        inferred_text: object = row["inferred_created_at"]
        created_at: datetime | None = datetime.fromisoformat(inferred_text) if isinstance(inferred_text, str) else None
        return ResolvedRun(
            run_id=str(row["legacy_run_id"]),
            path=Path(str(row["legacy_path"])),
            created_at=created_at,
            origin="legacy",
        )

    def _resolve_native(self, run_id: str) -> ResolvedRun | None:
        """Resolve a UUID4 manifest when the identifier is native."""
        try:
            parsed_id: uuid.UUID = uuid.UUID(run_id)
        except ValueError:
            return None
        if parsed_id.version != 4:
            return None
        manifest_path: Path = self._paths.runs_root / str(parsed_id) / RUN_MANIFEST_FILENAME
        if not manifest_path.is_file():
            return None
        try:
            raw: object = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError
            manifest = RunManifest(
                run_id=uuid.UUID(str(raw["run_id"])),
                created_at=datetime.fromisoformat(str(raw["created_at"])),
                schema_version=int(str(raw["schema_version"])),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise RunManifestReadError(f"Run manifest is invalid: {manifest_path}.") from error
        if manifest.run_id != parsed_id:
            raise RunManifestReadError(f"Run manifest identity does not match its directory: {manifest_path}.")
        return ResolvedRun(
            run_id=str(manifest.run_id),
            path=manifest_path.parent,
            created_at=manifest.created_at,
            origin="native",
        )
