"""Module containing durable source registry state for the money_pit package."""

from __future__ import annotations

# pyright: reportAny=false
import hashlib
import json
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING

from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


if TYPE_CHECKING:
    import sqlite3
from typing import cast


class SourceItemCollisionError(StorageError):
    """Raised when an item identity is reused for different canonical content."""


class SourceRepository:
    """Persist definitions, versioned items, and cursors through one database boundary."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to an initialized database."""
        self._database: Database = database

    def register_definition(
        self,
        definition: SourceDefinition,
        *,
        registry_version: int,
        registered_at: datetime,
    ) -> bool:
        """Insert or update one definition, returning whether durable state changed."""
        if registry_version < 1:
            raise ValueError("registry_version must be positive.")
        registered_at_text: str = _aware_utc_text(registered_at, field_name="registered_at")
        definition_json: str = json.dumps(
            definition.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        definition_hash: str = hashlib.sha256(definition_json.encode("utf-8")).hexdigest()
        with self._database.transaction(TransactionMode.WRITE) as connection:
            existing: sqlite3.Row | None = connection.execute(
                "SELECT definition_hash FROM source_definitions WHERE source_id = ?",
                (definition.source_id,),
            ).fetchone()
            if existing is not None and str(existing["definition_hash"]) == definition_hash:
                return False
            _ = connection.execute(
                """
                INSERT INTO source_definitions (
                    source_id, adapter_name, enabled, locator, cadence, tags_json,
                    trust_profile, allowed_uses_json, adapter_config_json,
                    registry_version, registered_at, definition_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    adapter_name = excluded.adapter_name,
                    enabled = excluded.enabled,
                    locator = excluded.locator,
                    cadence = excluded.cadence,
                    tags_json = excluded.tags_json,
                    trust_profile = excluded.trust_profile,
                    allowed_uses_json = excluded.allowed_uses_json,
                    adapter_config_json = excluded.adapter_config_json,
                    registry_version = excluded.registry_version,
                    registered_at = excluded.registered_at,
                    definition_hash = excluded.definition_hash
                """,
                (
                    definition.source_id,
                    definition.adapter_name,
                    int(definition.enabled),
                    definition.locator,
                    definition.cadence,
                    json.dumps(definition.tags),
                    definition.trust_profile,
                    json.dumps(definition.allowed_uses),
                    json.dumps(definition.adapter_config, sort_keys=True),
                    registry_version,
                    registered_at_text,
                    definition_hash,
                ),
            )
        return True

    def list_definitions(self) -> tuple[SourceDefinition, ...]:
        """Return all durable source definitions in stable identifier order."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = connection.execute(
                """
                SELECT source_id, adapter_name, enabled, locator, cadence, tags_json,
                       trust_profile, allowed_uses_json, adapter_config_json
                FROM source_definitions
                ORDER BY source_id
                """
            ).fetchall()
        return tuple(_definition_from_row(row) for row in rows)

    def get_definition(self, source_id: str) -> SourceDefinition | None:
        """Return one durable source definition when configured."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = connection.execute(
                """
                SELECT source_id, adapter_name, enabled, locator, cadence, tags_json,
                       trust_profile, allowed_uses_json, adapter_config_json
                FROM source_definitions
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
        return _definition_from_row(row) if row is not None else None

    def get_cursor(
        self,
        source_id: str,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> SourceCursor | None:
        """Return one durable discovery cursor when present."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = connection.execute(
                """
                SELECT cursor_json
                FROM source_cursors
                WHERE source_id = ? AND cursor_purpose = ?
                """,
                (source_id, purpose),
            ).fetchone()
        if row is None:
            return None
        try:
            return SourceCursor.model_validate_json(str(_column(row, "cursor_json")))
        except ValueError as error:
            raise StorageError("Stored source cursor is malformed.") from error

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
                existing: sqlite3.Row | None = connection.execute(
                    """
                    SELECT source_id, canonical_uri, published_at, updated_at, discovered_at
                    FROM source_items
                    WHERE source_item_id = ? AND content_version = ?
                    """,
                    (item.source_item_id, item.content_version),
                ).fetchone()
                item_values: tuple[str, str, str | None, str | None, str] = (
                    item.source_id,
                    item.canonical_uri,
                    _optional_aware_utc_text(item.published_at),
                    _optional_aware_utc_text(item.updated_at),
                    _aware_utc_text(item.discovered_at, field_name="discovered_at"),
                )
                if existing is not None:
                    existing_values: tuple[str, str, str | None, str | None, str] = (
                        str(existing["source_id"]),
                        str(existing["canonical_uri"]),
                        _optional_database_text(existing["published_at"]),
                        _optional_database_text(existing["updated_at"]),
                        str(existing["discovered_at"]),
                    )
                    if existing_values != item_values:
                        raise SourceItemCollisionError(
                            f"Source item {item.source_item_id!r} version {item.content_version!r} has different canonical content."
                        )
                    continue
                _ = connection.execute(
                    """
                    INSERT INTO source_items (
                        source_item_id, source_id, canonical_uri, published_at,
                        updated_at, discovered_at, content_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (item.source_item_id, *item_values, item.content_version),
                )
                inserted_count += 1
            if next_cursor is not None:
                _ = connection.execute(
                    """
                    INSERT INTO source_cursors (
                        source_id, cursor_purpose, cursor_json, updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source_id, cursor_purpose) DO UPDATE SET
                        cursor_json = excluded.cursor_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        source_id,
                        cursor_purpose,
                        next_cursor.model_dump_json(),
                        updated_at_text,
                    ),
                )
        return inserted_count

    def clear_cursor(
        self,
        source_id: str,
        *,
        purpose: SourceCursorPurpose,
    ) -> bool:
        """Delete one cursor timeline, returning whether it existed."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """
                DELETE FROM source_cursors
                WHERE source_id = ? AND cursor_purpose = ?
                """,
                (source_id, purpose),
            )
        return cursor.rowcount > 0


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])


def _definition_from_row(row: sqlite3.Row) -> SourceDefinition:
    try:
        return SourceDefinition(
            source_id=str(_column(row, "source_id")),
            adapter_name=str(_column(row, "adapter_name")),
            enabled=bool(_column(row, "enabled")),
            locator=str(_column(row, "locator")),
            cadence=_optional_database_text(_column(row, "cadence")),
            tags=tuple(json.loads(str(_column(row, "tags_json")))),
            trust_profile=_optional_database_text(_column(row, "trust_profile")),
            allowed_uses=tuple(json.loads(str(_column(row, "allowed_uses_json")))),
            adapter_config=json.loads(str(_column(row, "adapter_config_json"))),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StorageError("Stored source definition is malformed.") from error


def _aware_utc_text(value: datetime, *, field_name: str) -> str:
    """Return a UTC database timestamp from an aware datetime."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat()


def _optional_aware_utc_text(value: datetime | None) -> str | None:
    """Return an optional UTC database timestamp."""
    if value is None:
        return None
    return _aware_utc_text(value, field_name="source item timestamp")


def _optional_database_text(value: object) -> str | None:
    """Validate an optional text value read from SQLite."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("SQLite source timestamp must be text or null.")
    return value
