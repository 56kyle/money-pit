"""Module containing immutable source-policy and discovery persistence."""

from __future__ import annotations

# pyright: reportAny=false
import hashlib
import json
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import cast

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


if TYPE_CHECKING:
    import sqlite3


class SourceItemCollisionError(StorageError):
    """Raised when a source-item identity is reused for different content."""


class SourceCursorStatus(BaseModel):
    """Durable cursor state without inferred source health."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    source_id: str
    purpose: SourceCursorPurpose
    cursor: SourceCursor
    updated_at: AwareDatetime


class SourceRepository:
    """Persist immutable source definitions, versioned items, and cursors."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to one initialized database."""
        self._database: Database = database

    def register_definition(
        self,
        definition: SourceDefinition,
        *,
        registry_version: str,
        registered_at: datetime,
    ) -> bool:
        """Insert one immutable definition revision, returning whether it was new."""
        definition_json: str = _canonical_model_json(definition)
        definition_hash: str = hashlib.sha256(definition_json.encode("utf-8")).hexdigest()
        registered_at_text: str = _aware_utc_text(registered_at, field_name="registered_at")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor: sqlite3.Cursor = connection.execute(
                """
                INSERT OR IGNORE INTO source_definition_revisions (
                    definition_hash, source_id, registry_version, provenance_group,
                    definition_json, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    definition_hash,
                    definition.source_id,
                    registry_version,
                    definition.provenance_group,
                    definition_json,
                    registered_at_text,
                ),
            )
        return cursor.rowcount > 0

    def definition_hash(self, definition: SourceDefinition) -> str:
        """Return the durable identity of a validated source definition."""
        return hashlib.sha256(_canonical_model_json(definition).encode("utf-8")).hexdigest()

    def list_definitions(self) -> tuple[SourceDefinition, ...]:
        """Return the latest registered definition for every source."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = connection.execute(
                """
                SELECT definition_json
                FROM source_definition_revisions AS revision
                WHERE NOT EXISTS (
                    SELECT 1 FROM source_definition_revisions AS later
                    WHERE later.source_id = revision.source_id
                      AND (later.registered_at, later.definition_hash) >
                          (revision.registered_at, revision.definition_hash)
                )
                ORDER BY source_id
                """
            ).fetchall()
        return tuple(_definition_from_json(str(row["definition_json"])) for row in rows)

    def get_definition(self, source_id: str) -> SourceDefinition | None:
        """Return the latest registered definition for one source."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = connection.execute(
                """
                SELECT definition_json
                FROM source_definition_revisions
                WHERE source_id = ?
                ORDER BY registered_at DESC, definition_hash DESC
                LIMIT 1
                """,
                (source_id,),
            ).fetchone()
        return None if row is None else _definition_from_json(str(row["definition_json"]))

    def get_cursor(
        self, source_id: str, *, purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC
    ) -> SourceCursor | None:
        """Return one durable discovery cursor when present."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT cursor_json FROM source_cursors WHERE source_id = ? AND cursor_purpose = ?",
                (source_id, purpose.value),
            ).fetchone()
        if row is None:
            return None
        try:
            return SourceCursor.model_validate_json(str(row["cursor_json"]))
        except ValueError as error:
            raise StorageError("Stored source cursor is malformed.") from error

    def cursor_statuses(self, source_id: str | None = None) -> tuple[SourceCursorStatus, ...]:
        """Return persisted cursor values and timestamps through a strict read-only handle."""
        with self._database.read_only_transaction() as connection:
            if source_id is None:
                rows = connection.execute(
                    """SELECT source_id, cursor_purpose, cursor_json, updated_at
                    FROM source_cursors ORDER BY source_id, cursor_purpose"""
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT source_id, cursor_purpose, cursor_json, updated_at
                    FROM source_cursors WHERE source_id = ? ORDER BY cursor_purpose""",
                    (source_id,),
                ).fetchall()
        try:
            return tuple(
                SourceCursorStatus(
                    source_id=str(row["source_id"]),
                    purpose=SourceCursorPurpose(str(row["cursor_purpose"])),
                    cursor=SourceCursor.model_validate_json(str(row["cursor_json"])),
                    updated_at=datetime.fromisoformat(str(row["updated_at"])),
                )
                for row in rows
            )
        except ValueError as error:
            raise StorageError("Stored source cursor status is malformed.") from error

    def persist_discovery(
        self,
        source_id: str,
        items: tuple[SourceItem, ...],
        *,
        next_cursor: SourceCursor | None,
        updated_at: datetime,
        cursor_purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> int:
        """Persist idempotent items and the next cursor in one transaction."""
        updated_at_text: str = _aware_utc_text(updated_at, field_name="updated_at")
        inserted_count: int = 0
        with self._database.transaction(TransactionMode.WRITE) as connection:
            for item in items:
                if item.source_id != source_id:
                    raise ValueError(f"Item {item.source_item_id!r} belongs to {item.source_id!r}, not {source_id!r}.")
                item_values: tuple[object, ...] = (
                    item.source_item_id,
                    item.content_version,
                    item.source_id,
                    item.source_definition_hash,
                    item.canonical_uri,
                    _optional_aware_utc_text(item.published_at),
                    _optional_aware_utc_text(item.updated_at),
                    _aware_utc_text(item.discovered_at, field_name="discovered_at"),
                )
                cursor: sqlite3.Cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO source_items (
                        source_item_id, content_version, source_id, source_definition_hash,
                        canonical_uri, published_at, updated_at, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    item_values,
                )
                if cursor.rowcount > 0:
                    inserted_count += 1
                    continue
                row: sqlite3.Row = cast(
                    "sqlite3.Row",
                    connection.execute(
                        """
                        SELECT source_item_id, content_version, source_id, source_definition_hash,
                               canonical_uri, published_at, updated_at, discovered_at
                        FROM source_items WHERE source_item_id = ? AND content_version = ?
                        """,
                        (item.source_item_id, item.content_version),
                    ).fetchone(),
                )
                if tuple(row) != item_values:
                    raise SourceItemCollisionError(
                        f"Source item {item.source_item_id!r} version {item.content_version!r} has different content."
                    )
            if next_cursor is not None:
                _ = connection.execute(
                    """
                    INSERT INTO source_cursors (source_id, cursor_purpose, cursor_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source_id, cursor_purpose) DO UPDATE SET
                        cursor_json = excluded.cursor_json,
                        updated_at = excluded.updated_at
                    """,
                    (source_id, cursor_purpose.value, next_cursor.model_dump_json(), updated_at_text),
                )
        return inserted_count

    def clear_cursor(self, source_id: str, *, purpose: SourceCursorPurpose) -> bool:
        """Delete one cursor timeline, returning whether it existed."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor: sqlite3.Cursor = connection.execute(
                "DELETE FROM source_cursors WHERE source_id = ? AND cursor_purpose = ?",
                (source_id, purpose.value),
            )
        return cursor.rowcount > 0


def _canonical_model_json(definition: SourceDefinition) -> str:
    return json.dumps(definition.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _definition_from_json(value: str) -> SourceDefinition:
    try:
        return SourceDefinition.model_validate_json(value)
    except ValueError as error:
        raise StorageError("Stored source definition is malformed.") from error


def _aware_utc_text(value: datetime, *, field_name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat()


def _optional_aware_utc_text(value: datetime | None) -> str | None:
    return None if value is None else _aware_utc_text(value, field_name="source item timestamp")
