"""Module containing the read-only legacy daily-show indexer for money_pit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from enum import StrEnum
from typing import TYPE_CHECKING

from money_pit.constants import FILE_SAFE_DATETIME_FORMAT
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


class LegacyImportStatus(StrEnum):
    """Outcome of indexing one legacy directory."""

    INDEXED = "indexed"
    ALREADY_INDEXED = "already_indexed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class LegacyImportDiagnostic:
    """A structured, path-specific legacy indexing outcome."""

    path: Path
    status: LegacyImportStatus
    detail: str


@dataclass(frozen=True)
class LegacyImportReport:
    """The complete diagnostics from one read-only legacy scan."""

    legacy_root: Path
    scanned_count: int
    indexed_count: int
    already_indexed_count: int
    diagnostics: tuple[LegacyImportDiagnostic, ...]


class LegacyDailyShowImporter:
    """Index legacy daily-show directories without modifying their contents."""

    def __init__(self, database: Database, legacy_root: Path) -> None:
        """Bind the indexer to one database and legacy root."""
        self._database: Database = database
        self._legacy_root: Path = legacy_root

    def index(self, *, indexed_at: datetime | None = None) -> LegacyImportReport:
        """Index each immediate legacy run directory idempotently."""
        if not self._legacy_root.exists():
            return _unavailable_report(self._legacy_root, "Legacy root does not exist.")
        if not self._legacy_root.is_dir():
            return _unavailable_report(self._legacy_root, "Legacy root is not a directory.")

        scan_instant: datetime = indexed_at if indexed_at is not None else datetime.now(tz=timezone.utc)
        if scan_instant.tzinfo is None or scan_instant.utcoffset() is None:
            raise ValueError("indexed_at must be timezone-aware.")
        candidates: tuple[Path, ...] = tuple(
            sorted(
                (path for path in self._legacy_root.iterdir() if path.is_dir()),
                key=lambda path: path.name,
            )
        )
        diagnostics: list[LegacyImportDiagnostic] = []
        with self._database.transaction(TransactionMode.WRITE) as connection:
            for candidate in candidates:
                canonical_path: str = str(candidate.resolve())
                legacy_run_id: str = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
                artifact_names: tuple[str, ...] = tuple(sorted(child.name for child in candidate.iterdir()))
                inferred_created_at: str | None = _infer_created_at(candidate.name)
                cursor: sqlite3.Cursor = connection.execute(
                    """
                    INSERT INTO legacy_runs (
                        legacy_run_id,
                        legacy_path,
                        slug,
                        inferred_created_at,
                        indexed_at,
                        artifact_names_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(legacy_path) DO NOTHING
                    """,
                    (
                        legacy_run_id,
                        canonical_path,
                        candidate.name,
                        inferred_created_at,
                        scan_instant.astimezone(timezone.utc).isoformat(),
                        json.dumps(artifact_names),
                    ),
                )
                status: LegacyImportStatus = (
                    LegacyImportStatus.INDEXED if cursor.rowcount == 1 else LegacyImportStatus.ALREADY_INDEXED
                )
                detail: str = (
                    "Indexed legacy directory."
                    if status is LegacyImportStatus.INDEXED
                    else "Legacy directory was already indexed."
                )
                if inferred_created_at is None:
                    detail = f"{detail} The slug does not encode a recognized creation time."
                diagnostics.append(LegacyImportDiagnostic(path=candidate, status=status, detail=detail))

        indexed_count: int = sum(diagnostic.status is LegacyImportStatus.INDEXED for diagnostic in diagnostics)
        already_indexed_count: int = sum(
            diagnostic.status is LegacyImportStatus.ALREADY_INDEXED for diagnostic in diagnostics
        )
        return LegacyImportReport(
            legacy_root=self._legacy_root,
            scanned_count=len(candidates),
            indexed_count=indexed_count,
            already_indexed_count=already_indexed_count,
            diagnostics=tuple(diagnostics),
        )


def _unavailable_report(legacy_root: Path, detail: str) -> LegacyImportReport:
    """Return structured diagnostics for an unavailable legacy root."""
    return LegacyImportReport(
        legacy_root=legacy_root,
        scanned_count=0,
        indexed_count=0,
        already_indexed_count=0,
        diagnostics=(
            LegacyImportDiagnostic(
                path=legacy_root,
                status=LegacyImportStatus.SKIPPED,
                detail=detail,
            ),
        ),
    )


def _infer_created_at(slug: str) -> str | None:
    """Return a UTC instant when a legacy slug uses the old timestamp format."""
    try:
        aware: datetime = datetime.strptime(slug, FILE_SAFE_DATETIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return aware.isoformat()
