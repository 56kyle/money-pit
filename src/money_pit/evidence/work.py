"""Module adapting durable evidence to A1 interpretation work."""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Literal
from typing import cast
from uuid import uuid4

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import TypeAdapter

from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import EvidenceLocator
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


if TYPE_CHECKING:
    import sqlite3


_LOCATOR_ADAPTER: TypeAdapter[EvidenceLocator] = TypeAdapter(EvidenceLocator)
_RETRY_DELAYS: tuple[timedelta, ...] = (timedelta(minutes=5), timedelta(minutes=20))


class EvidenceInterpretationWork(BaseModel):
    """One exact acquisition version selected for A1 interpretation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    document: EvidenceDocument
    content_version: str


class ReusableInterpretation(BaseModel):
    """One exact completed interpretation reconstructed without model execution."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str
    document_id: str
    fragment_ids: tuple[str, ...]
    observations: tuple[ClaimObservation, ...]
    known_at: datetime
    completed_at: datetime


class EvidenceWorkStore:
    """Provide point-in-time evidence and durable A1 interpretation attempts."""

    def __init__(self, database: Database) -> None:
        """Bind A1 work persistence to an initialized database."""
        self._database: Database = database

    def list_pending_documents(
        self,
        *,
        as_of: datetime,
        source_id: str | None,
        limit: int,
        interpreter_version: str,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        """Return bounded acquisitions without a successful interpreter-version attempt."""
        if limit <= 0:
            raise ValueError("Evidence work limit must be positive")
        parameters: list[object] = [interpreter_version, _utc_text(as_of), _utc_text(as_of)]
        with self._database.transaction() as connection:
            if source_id is None:
                parameters.append(limit)
                rows = connection.execute(
                    """
                    WITH retry AS (
                        SELECT source_item_id, content_version, asset_id,
                               MAX(CASE WHEN outcome = 'failed' THEN retry_after END) AS retry_after,
                               MAX(CASE WHEN outcome IN ('pending', 'succeeded', 'quarantined')
                                        THEN 1 ELSE 0 END) AS blocked
                        FROM claim_interpretation_attempts
                        WHERE interpreter_version = ?
                        GROUP BY source_item_id, content_version, asset_id
                    )
                    SELECT acquisition.asset_id, acquisition.source_item_id,
                           acquisition.content_version, acquisition.retrieved_at,
                           acquisition.media_type, asset.content_hash, asset.local_path
                    FROM evidence_asset_acquisitions AS acquisition
                    JOIN evidence_assets AS asset ON asset.asset_id = acquisition.asset_id
                    JOIN source_items AS item
                      ON item.source_item_id = acquisition.source_item_id
                     AND item.content_version = acquisition.content_version
                    JOIN source_definition_revisions AS revision
                      ON revision.definition_hash = acquisition.source_definition_hash
                    LEFT JOIN retry
                      ON retry.source_item_id = acquisition.source_item_id
                     AND retry.content_version = acquisition.content_version
                     AND retry.asset_id = acquisition.asset_id
                    WHERE acquisition.retrieved_at <= ?
                      AND EXISTS (
                          SELECT 1
                          FROM json_each(revision.definition_json, '$.allowed_uses') AS allowed_use
                          WHERE allowed_use.value = 'interpretation'
                      )
                      AND EXISTS (
                          SELECT 1 FROM evidence_processing_attempts AS processing
                          WHERE processing.source_item_id = acquisition.source_item_id
                            AND processing.content_version = acquisition.content_version
                            AND processing.asset_id = acquisition.asset_id
                            AND processing.status = 'succeeded'
                      )
                      AND COALESCE(retry.blocked, 0) = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM legacy_interpretation_reuse AS legacy
                          WHERE legacy.source_item_id = acquisition.source_item_id
                            AND legacy.content_version = acquisition.content_version
                            AND legacy.asset_id = acquisition.asset_id
                      )
                      AND (retry.retry_after IS NULL OR retry.retry_after <= ?)
                    ORDER BY COALESCE(retry.retry_after, acquisition.retrieved_at), acquisition.source_item_id,
                             acquisition.content_version, acquisition.asset_id
                    LIMIT ?
                    """,
                    tuple(parameters),
                ).fetchall()
            else:
                parameters.extend((source_id, limit))
                rows = connection.execute(
                    """
                    WITH retry AS (
                        SELECT source_item_id, content_version, asset_id,
                               MAX(CASE WHEN outcome = 'failed' THEN retry_after END) AS retry_after,
                               MAX(CASE WHEN outcome IN ('pending', 'succeeded', 'quarantined')
                                        THEN 1 ELSE 0 END) AS blocked
                        FROM claim_interpretation_attempts
                        WHERE interpreter_version = ?
                        GROUP BY source_item_id, content_version, asset_id
                    )
                    SELECT acquisition.asset_id, acquisition.source_item_id,
                           acquisition.content_version, acquisition.retrieved_at,
                           acquisition.media_type, asset.content_hash, asset.local_path
                    FROM evidence_asset_acquisitions AS acquisition
                    JOIN evidence_assets AS asset ON asset.asset_id = acquisition.asset_id
                    JOIN source_items AS item
                      ON item.source_item_id = acquisition.source_item_id
                     AND item.content_version = acquisition.content_version
                    JOIN source_definition_revisions AS revision
                      ON revision.definition_hash = acquisition.source_definition_hash
                    LEFT JOIN retry
                      ON retry.source_item_id = acquisition.source_item_id
                     AND retry.content_version = acquisition.content_version
                     AND retry.asset_id = acquisition.asset_id
                    WHERE acquisition.retrieved_at <= ?
                      AND EXISTS (
                          SELECT 1
                          FROM json_each(revision.definition_json, '$.allowed_uses') AS allowed_use
                          WHERE allowed_use.value = 'interpretation'
                      )
                      AND EXISTS (
                          SELECT 1 FROM evidence_processing_attempts AS processing
                          WHERE processing.source_item_id = acquisition.source_item_id
                            AND processing.content_version = acquisition.content_version
                            AND processing.asset_id = acquisition.asset_id
                            AND processing.status = 'succeeded'
                      )
                      AND COALESCE(retry.blocked, 0) = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM legacy_interpretation_reuse AS legacy
                          WHERE legacy.source_item_id = acquisition.source_item_id
                            AND legacy.content_version = acquisition.content_version
                            AND legacy.asset_id = acquisition.asset_id
                      )
                      AND (retry.retry_after IS NULL OR retry.retry_after <= ?)
                      AND item.source_id = ?
                    ORDER BY COALESCE(retry.retry_after, acquisition.retrieved_at), acquisition.source_item_id,
                             acquisition.content_version, acquisition.asset_id
                    LIMIT ?
                    """,
                    tuple(parameters),
                ).fetchall()
            acquisitions: list[sqlite3.Row] = cast("list[sqlite3.Row]", rows)
            work_items: list[EvidenceInterpretationWork] = []
            for acquisition in acquisitions:
                asset_id: str = str(_column(acquisition, "asset_id"))
                fragment_rows: list[sqlite3.Row] = cast(
                    "list[sqlite3.Row]",
                    connection.execute(
                        """
                        SELECT fragment_id, fragment_kind, locator_json, extracted_text,
                               cited_source_text, extraction_method, extraction_model, confidence
                        FROM evidence_fragments WHERE asset_id = ? ORDER BY fragment_id
                        """,
                        (asset_id,),
                    ).fetchall(),
                )
                work_items.append(
                    EvidenceInterpretationWork(
                        document=_document_from_rows(acquisition, fragment_rows),
                        content_version=str(_column(acquisition, "content_version")),
                    ),
                )
        return tuple(work_items)

    def documents_for_bundle(
        self,
        *,
        source_item_id: str,
        content_version: str,
        processor_name: str | None = None,
        processor_version: str | None = None,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        """Return every processed document for one exact source-item version."""
        if (processor_name is None) != (processor_version is None):
            raise ValueError("Processor name and version must be supplied together")
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """
                    SELECT acquisition.asset_id, acquisition.source_item_id,
                           acquisition.content_version, acquisition.retrieved_at,
                           acquisition.media_type, asset.content_hash, asset.local_path
                    FROM evidence_asset_acquisitions AS acquisition
                    JOIN evidence_assets AS asset ON asset.asset_id = acquisition.asset_id
                    WHERE acquisition.source_item_id = ? AND acquisition.content_version = ?
                      AND EXISTS (
                          SELECT 1 FROM evidence_processing_attempts AS processing
                          WHERE processing.source_item_id = acquisition.source_item_id
                            AND processing.content_version = acquisition.content_version
                            AND processing.asset_id = acquisition.asset_id
                            AND processing.status = 'succeeded'
                            AND (? IS NULL OR (
                                processing.processor_name = ?
                                AND processing.processor_version = ?
                            ))
                      )
                    ORDER BY acquisition.asset_id
                    """,
                    (
                        source_item_id,
                        content_version,
                        processor_name,
                        processor_name,
                        processor_version,
                    ),
                ).fetchall(),
            )
            work_items: list[EvidenceInterpretationWork] = []
            for acquisition in rows:
                asset_id = str(_column(acquisition, "asset_id"))
                fragment_rows: list[sqlite3.Row] = cast(
                    "list[sqlite3.Row]",
                    connection.execute(
                        """SELECT fragment_id, fragment_kind, locator_json, extracted_text,
                                  cited_source_text, extraction_method, extraction_model, confidence
                           FROM evidence_fragments WHERE asset_id = ? ORDER BY fragment_id""",
                        (asset_id,),
                    ).fetchall(),
                )
                work_items.append(
                    EvidenceInterpretationWork(
                        document=_document_from_rows(acquisition, fragment_rows),
                        content_version=content_version,
                    )
                )
        return tuple(work_items)

    def document_for_asset(
        self,
        *,
        source_item_id: str,
        asset_id: str,
    ) -> EvidenceInterpretationWork:
        """Return the exact processed acquisition for a durable research fetch."""
        with self._database.transaction() as connection:
            rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT acquisition.asset_id, acquisition.source_item_id,
                acquisition.content_version, acquisition.retrieved_at, acquisition.media_type,
                asset.content_hash, asset.local_path
                FROM evidence_asset_acquisitions AS acquisition
                JOIN evidence_assets AS asset USING (asset_id)
                WHERE acquisition.source_item_id = ? AND acquisition.asset_id = ?
                  AND EXISTS (SELECT 1 FROM evidence_processing_attempts AS processing
                    WHERE processing.source_item_id = acquisition.source_item_id
                      AND processing.content_version = acquisition.content_version
                      AND processing.asset_id = acquisition.asset_id
                      AND processing.status = 'succeeded')
                ORDER BY acquisition.retrieved_at DESC, acquisition.content_version DESC""",
                    (source_item_id, asset_id),
                ).fetchall(),
            )
            if not rows:
                raise KeyError((source_item_id, asset_id))
            acquisition = rows[0]
            fragments = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT fragment_id, fragment_kind, locator_json, extracted_text,
                cited_source_text, extraction_method, extraction_model, confidence
                FROM evidence_fragments WHERE asset_id = ? ORDER BY fragment_id""",
                    (asset_id,),
                ).fetchall(),
            )
        return EvidenceInterpretationWork(
            document=_document_from_rows(acquisition, fragments),
            content_version=str(_column(acquisition, "content_version")),
        )

    def reusable_interpretation(
        self,
        work: EvidenceInterpretationWork,
        *,
        interpreter_version: str,
    ) -> ReusableInterpretation | None:
        """Return the exact successful interpretation for one work and interpreter identity."""
        document = work.document
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                    SELECT attempt_id, observation_ids_json, known_at, completed_at
                    FROM claim_interpretation_attempts
                    WHERE source_item_id = ? AND content_version = ? AND asset_id = ?
                      AND interpreter_version = ? AND outcome = 'succeeded'
                    ORDER BY completed_at DESC, attempt_id DESC LIMIT 1
                    """,
                    (
                        document.asset.source_item_id,
                        work.content_version,
                        document.asset.asset_id,
                        interpreter_version,
                    ),
                ).fetchone(),
            )
            if row is None:
                return None
            observation_ids_raw: object = cast(
                "object",
                json.loads(str(_column(row, "observation_ids_json"))),
            )
            if not isinstance(observation_ids_raw, list):
                raise ValueError("Stored interpretation observation identities are malformed")
            validated_ids = cast("list[object]", observation_ids_raw)
            if not all(isinstance(value, str) for value in validated_ids):
                raise ValueError("Stored interpretation observation identities are malformed")
            observation_ids: tuple[str, ...] = tuple(value for value in validated_ids if isinstance(value, str))
            observations: list[ClaimObservation] = []
            for observation_id in observation_ids:
                observation_row: sqlite3.Row | None = cast(
                    "sqlite3.Row | None",
                    connection.execute(
                        "SELECT observation_json FROM claim_observations WHERE observation_id = ?",
                        (observation_id,),
                    ).fetchone(),
                )
                if observation_row is None:
                    raise ValueError("Stored interpretation references a missing observation")
                observations.append(
                    ClaimObservation.model_validate_json(
                        str(_column(observation_row, "observation_json")),
                    ),
                )
        known_at_raw: object = _column(row, "known_at")
        completed_at_raw: object = _column(row, "completed_at")
        if known_at_raw is None or completed_at_raw is None:
            raise ValueError("Successful interpretation timestamps are incomplete")
        return ReusableInterpretation(
            attempt_id=str(_column(row, "attempt_id")),
            document_id=document.asset.asset_id,
            fragment_ids=tuple(fragment.fragment_id for fragment in document.fragments),
            observations=tuple(observations),
            known_at=datetime.fromisoformat(str(known_at_raw)),
            completed_at=datetime.fromisoformat(str(completed_at_raw)),
        )

    def preserved_legacy_interpretation(
        self,
        work: EvidenceInterpretationWork,
    ) -> ReusableInterpretation | None:
        """Return migrated predecessor work without claiming current-policy equivalence."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """SELECT attempt.interpreter_version
                    FROM legacy_interpretation_reuse AS legacy
                    JOIN claim_interpretation_attempts AS attempt ON attempt.attempt_id = legacy.attempt_id
                    WHERE legacy.source_item_id = ? AND legacy.content_version = ? AND legacy.asset_id = ?""",
                    (
                        work.document.asset.source_item_id,
                        work.content_version,
                        work.document.asset.asset_id,
                    ),
                ).fetchone(),
            )
        if row is None:
            return None
        return self.reusable_interpretation(work, interpreter_version=str(_column(row, "interpreter_version")))

    def begin_interpretation(
        self,
        work: EvidenceInterpretationWork,
        *,
        run_id: str,
        interpreter_version: str,
        started_at: datetime,
    ) -> str:
        """Persist a pending interpretation attempt for an exact acquisition."""
        attempt_id: str = str(uuid4())
        document: EvidenceDocument = work.document
        with self._database.transaction(TransactionMode.WRITE) as connection:
            acquisition: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                    SELECT acquisition.acquisition_id
                    FROM evidence_asset_acquisitions AS acquisition
                    JOIN source_definition_revisions AS revision
                      ON revision.definition_hash = acquisition.source_definition_hash
                    WHERE acquisition.source_item_id = ?
                      AND acquisition.content_version = ?
                      AND acquisition.asset_id = ?
                      AND EXISTS (
                          SELECT 1
                          FROM json_each(revision.definition_json, '$.allowed_uses') AS allowed_use
                          WHERE allowed_use.value = 'interpretation'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM claim_interpretation_attempts AS attempt
                          WHERE attempt.source_item_id = acquisition.source_item_id
                            AND attempt.content_version = acquisition.content_version
                            AND attempt.asset_id = acquisition.asset_id
                            AND attempt.interpreter_version = ?
                            AND attempt.outcome IN ('pending', 'succeeded', 'quarantined')
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM claim_interpretation_attempts AS attempt
                          WHERE attempt.source_item_id = acquisition.source_item_id
                            AND attempt.content_version = acquisition.content_version
                            AND attempt.asset_id = acquisition.asset_id
                            AND attempt.interpreter_version = ?
                            AND attempt.outcome = 'failed'
                            AND attempt.retry_after > ?
                      )
                    """,
                    (
                        document.asset.source_item_id,
                        work.content_version,
                        document.asset.asset_id,
                        interpreter_version,
                        interpreter_version,
                        _utc_text(started_at),
                    ),
                ).fetchone(),
            )
            if acquisition is None:
                raise KeyError(document.asset.asset_id)
            _ = connection.execute(
                """
                INSERT INTO claim_interpretation_attempts (
                    attempt_id, source_item_id, content_version, asset_id, run_id,
                    interpreter_version, started_at, outcome, observation_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '[]')
                """,
                (
                    attempt_id,
                    document.asset.source_item_id,
                    work.content_version,
                    document.asset.asset_id,
                    run_id,
                    interpreter_version,
                    _utc_text(started_at),
                ),
            )
        return attempt_id

    def complete_interpretation(
        self,
        attempt_id: str,
        *,
        observation_ids: tuple[str, ...],
        known_at: datetime,
        completed_at: datetime,
    ) -> None:
        """Complete an interpretation, including a zero-observation result."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """
                UPDATE claim_interpretation_attempts
                SET completed_at = ?, known_at = ?, outcome = 'succeeded',
                    observation_ids_json = ?
                WHERE attempt_id = ? AND outcome = 'pending'
                """,
                (
                    _utc_text(completed_at),
                    _utc_text(known_at),
                    json.dumps(observation_ids, separators=(",", ":")),
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(attempt_id)

    def fail_interpretation(
        self,
        attempt_id: str,
        *,
        failure_kind: str,
        completed_at: datetime,
    ) -> None:
        """Schedule a bounded retry or quarantine the exact interpreter work."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            attempt: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                    SELECT source_item_id, content_version, asset_id, interpreter_version
                    FROM claim_interpretation_attempts
                    WHERE attempt_id = ? AND outcome = 'pending'
                    """,
                    (attempt_id,),
                ).fetchone(),
            )
            if attempt is None:
                raise KeyError(attempt_id)
            failure_count_row: sqlite3.Row = cast(
                "sqlite3.Row",
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM claim_interpretation_attempts
                    WHERE source_item_id = ? AND content_version = ? AND asset_id = ?
                      AND interpreter_version = ? AND outcome IN ('failed', 'quarantined')
                    """,
                    (
                        _column(attempt, "source_item_id"),
                        _column(attempt, "content_version"),
                        _column(attempt, "asset_id"),
                        _column(attempt, "interpreter_version"),
                    ),
                ).fetchone(),
            )
            previous_failures = int(str(cast("object", failure_count_row[0])))
            outcome = "quarantined" if previous_failures >= len(_RETRY_DELAYS) else "failed"
            retry_after = (
                None if outcome == "quarantined" else _utc_text(completed_at + _RETRY_DELAYS[previous_failures])
            )
            cursor = connection.execute(
                """
                UPDATE claim_interpretation_attempts
                SET completed_at = ?, known_at = ?, outcome = ?, failure_kind = ?, retry_after = ?
                WHERE attempt_id = ? AND outcome = 'pending'
                """,
                (
                    _utc_text(completed_at),
                    _utc_text(completed_at),
                    outcome,
                    failure_kind,
                    retry_after,
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(attempt_id)


def _document_from_rows(
    acquisition: sqlite3.Row,
    fragment_rows: list[sqlite3.Row],
) -> EvidenceDocument:
    asset_id: str = str(_column(acquisition, "asset_id"))
    fragments: tuple[EvidenceFragment, ...] = tuple(
        EvidenceFragment(
            fragment_id=str(_column(row, "fragment_id")),
            asset_id=asset_id,
            kind=_fragment_kind(_column(row, "fragment_kind")),
            locator=_LOCATOR_ADAPTER.validate_json(str(_column(row, "locator_json"))),
            extracted_text=_optional_text(_column(row, "extracted_text")),
            cited_source_text=_optional_text(_column(row, "cited_source_text")),
            extraction_method=str(_column(row, "extraction_method")),
            extraction_model=_optional_text(_column(row, "extraction_model")),
            confidence=_optional_float(_column(row, "confidence")),
        )
        for row in fragment_rows
    )
    return EvidenceDocument(
        asset=EvidenceAsset(
            asset_id=asset_id,
            content_hash=str(_column(acquisition, "content_hash")),
            media_type=str(_column(acquisition, "media_type")),
            source_item_id=str(_column(acquisition, "source_item_id")),
            local_path=Path(str(_column(acquisition, "local_path"))),
            retrieved_at=datetime.fromisoformat(str(_column(acquisition, "retrieved_at"))),
        ),
        fragments=fragments,
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(str(value))


def _fragment_kind(value: object) -> Literal["transcript", "frame", "page", "web_span", "table"]:
    text: str = str(value)
    if text == "transcript":
        return "transcript"
    if text == "frame":
        return "frame"
    if text == "page":
        return "page"
    if text == "web_span":
        return "web_span"
    if text == "table":
        return "table"
    raise ValueError("Stored evidence fragment kind is invalid")


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Evidence work timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])
