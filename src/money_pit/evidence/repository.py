"""Module containing durable evidence-processing attempt persistence."""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING
from typing import cast

from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


if TYPE_CHECKING:
    import sqlite3

    from money_pit.schemas.evidence import EvidenceProcessingAttempt


class EvidenceProcessingAttemptRepository:
    """Persist immutable terminal processor attempts idempotently."""

    def __init__(self, database: Database) -> None:
        """Bind attempt persistence to an initialized database."""
        self._database: Database = database

    def persist(self, attempt: EvidenceProcessingAttempt) -> bool:
        """Persist one exact attempt, returning whether it was new."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            existing: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                    SELECT source_item_id, content_version, asset_id, processor_name,
                           processor_version, started_at, completed_at, status,
                           failure_kind, document_id, fragment_ids_json
                    FROM evidence_processing_attempts WHERE attempt_id = ?
                    """,
                    (attempt.attempt_id,),
                ).fetchone(),
            )
            values: tuple[object, ...] = _attempt_values(attempt)
            if existing is not None:
                durable: tuple[object, ...] = tuple(
                    _column(existing, column)
                    for column in (
                        "source_item_id",
                        "content_version",
                        "asset_id",
                        "processor_name",
                        "processor_version",
                        "started_at",
                        "completed_at",
                        "status",
                        "failure_kind",
                        "document_id",
                        "fragment_ids_json",
                    )
                )
                if durable != values:
                    raise ValueError("Evidence processing attempt identity collision")
                return False
            _ = connection.execute(
                """
                INSERT INTO evidence_processing_attempts (
                    attempt_id, source_item_id, content_version, asset_id, processor_name,
                    processor_version, started_at, completed_at, status,
                    failure_kind, document_id, fragment_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (attempt.attempt_id, *values),
            )
        return True


def _attempt_values(attempt: EvidenceProcessingAttempt) -> tuple[object, ...]:
    return (
        attempt.source_item_id,
        attempt.content_version,
        attempt.asset_id,
        attempt.processor_name,
        attempt.processor_version,
        _utc_text(attempt.started_at),
        _utc_text(attempt.completed_at) if attempt.completed_at is not None else None,
        attempt.status,
        attempt.failure_kind,
        attempt.document_id,
        json.dumps(attempt.fragment_ids, separators=(",", ":")),
    )


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Evidence processing timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])
