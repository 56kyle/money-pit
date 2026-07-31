"""Module orchestrating durable source discovery and evidence extraction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Protocol
from typing import cast

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.sources._shared import utc_now
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from money_pit.schemas.evidence import EvidenceDocument
    from money_pit.schemas.evidence import EvidenceFragment
    from money_pit.schemas.sources import SourceDefinition
    from money_pit.schemas.sources import SourceItem
    from money_pit.schemas.sources import SourceRegistryDocument
    from money_pit.sources.registry import AdapterRegistry
    from money_pit.storage.assets import AssetStore


class SourceStateRepository(Protocol):
    """Durable source state required by synchronization."""

    def register_definition(
        self,
        definition: SourceDefinition,
        *,
        registry_version: int,
        registered_at: datetime,
    ) -> bool:
        """Persist one current source definition."""
        ...

    def get_cursor(
        self,
        source_id: str,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> SourceCursor | None:
        """Return the last committed discovery cursor."""
        ...

    def clear_cursor(
        self,
        source_id: str,
        *,
        purpose: SourceCursorPurpose,
    ) -> bool:
        """Delete one cursor timeline, returning whether it existed."""
        ...


class SourceSyncResult(BaseModel):
    """Durable outcome of synchronizing one configured source."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    discovered_count: int = Field(ge=0)
    persisted_count: int = Field(ge=0)
    evidence_document_count: int = Field(ge=0)
    next_cursor: SourceCursor | None


@dataclass(frozen=True)
class SourceIngestionRecord:
    """One fetched source-item version and its extracted evidence."""

    source_item: SourceItem
    evidence_document: EvidenceDocument


class SourceIngestionPersistenceResult(BaseModel):
    """Counts committed by one atomic source-ingestion transaction."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    persisted_item_count: int = Field(ge=0)
    changed_evidence_document_count: int = Field(ge=0)


class EvidenceRepository:
    """Persist extracted evidence and its full-text search projection."""

    def __init__(self, database: Database) -> None:
        """Bind evidence persistence to an initialized database."""
        self._database: Database = database

    def persist(
        self,
        document: EvidenceDocument,
        *,
        source_item: SourceItem,
    ) -> bool:
        """Persist one exact item-version acquisition and its immutable fragments."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            self._require_source_item(connection, source_item)
            asset_inserted: bool = self._persist_asset(connection, document)
            acquisition_inserted: bool = self._persist_acquisition(
                connection,
                document,
                source_item=source_item,
            )
            fragment_inserted: bool = False
            for fragment in document.fragments:
                inserted: bool = self._persist_fragment(connection, fragment)
                fragment_inserted = inserted or fragment_inserted
        return asset_inserted or acquisition_inserted or fragment_inserted

    def persist_ingestion_batch(
        self,
        source_id: str,
        records: tuple[SourceIngestionRecord, ...],
        *,
        next_cursor: SourceCursor | None,
        updated_at: datetime,
        cursor_purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> SourceIngestionPersistenceResult:
        """Persist item versions, evidence, search rows, and cursor atomically."""
        persisted_item_count: int = 0
        changed_evidence_document_count: int = 0
        with self._database.transaction(TransactionMode.WRITE) as connection:
            for record in records:
                item: SourceItem = record.source_item
                if item.source_id != source_id:
                    raise SourceDiscoveryError(
                        f"Item {item.source_item_id!r} belongs to {item.source_id!r}, not {source_id!r}"
                    )
                if self._persist_source_item(connection, item):
                    persisted_item_count += 1
                document: EvidenceDocument = record.evidence_document
                changed: bool = self._persist_asset(connection, document)
                changed = (
                    self._persist_acquisition(
                        connection,
                        document,
                        source_item=item,
                    )
                    or changed
                )
                for fragment in document.fragments:
                    changed = self._persist_fragment(connection, fragment) or changed
                if changed:
                    changed_evidence_document_count += 1
            self._persist_cursor(
                connection,
                source_id,
                next_cursor=next_cursor,
                updated_at=updated_at,
                cursor_purpose=cursor_purpose,
            )
        return SourceIngestionPersistenceResult(
            persisted_item_count=persisted_item_count,
            changed_evidence_document_count=changed_evidence_document_count,
        )

    @staticmethod
    def _persist_source_item(
        connection: sqlite3.Connection,
        item: SourceItem,
    ) -> bool:
        existing: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                """
                SELECT source_id, canonical_uri, published_at, updated_at, discovered_at
                FROM source_items
                WHERE source_item_id = ? AND content_version = ?
                """,
                (item.source_item_id, item.content_version),
            ).fetchone(),
        )
        values: tuple[str, str, str | None, str | None, str] = (
            item.source_id,
            item.canonical_uri,
            _optional_utc_text(item.published_at),
            _optional_utc_text(item.updated_at),
            _utc_text(item.discovered_at),
        )
        if existing is not None:
            durable_values: tuple[str, str, str | None, str | None, str] = (
                str(_column(existing, "source_id")),
                str(_column(existing, "canonical_uri")),
                _optional_text(_column(existing, "published_at")),
                _optional_text(_column(existing, "updated_at")),
                str(_column(existing, "discovered_at")),
            )
            if durable_values != values:
                raise SourceDiscoveryError("Source item version identity collision")
            return False
        _ = connection.execute(
            """
            INSERT INTO source_items (
                source_item_id, source_id, canonical_uri, published_at,
                updated_at, discovered_at, content_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (item.source_item_id, *values, item.content_version),
        )
        return True

    @staticmethod
    def _require_source_item(
        connection: sqlite3.Connection,
        item: SourceItem,
    ) -> None:
        row: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                """
                SELECT source_item_id
                FROM source_items
                WHERE source_item_id = ? AND content_version = ?
                """,
                (item.source_item_id, item.content_version),
            ).fetchone(),
        )
        if row is None:
            raise SourceDiscoveryError("Evidence acquisition references an unknown source item version")

    @staticmethod
    def _persist_cursor(
        connection: sqlite3.Connection,
        source_id: str,
        *,
        next_cursor: SourceCursor | None,
        updated_at: datetime,
        cursor_purpose: SourceCursorPurpose,
    ) -> None:
        if next_cursor is None:
            return
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
                _utc_text(updated_at),
            ),
        )

    @staticmethod
    def _persist_asset(
        connection: sqlite3.Connection,
        document: EvidenceDocument,
    ) -> bool:
        existing: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                """
                SELECT content_hash, local_path
                FROM evidence_assets
                WHERE asset_id = ?
                """,
                (document.asset.asset_id,),
            ).fetchone(),
        )
        canonical_values: tuple[str, str] = (
            document.asset.content_hash,
            str(document.asset.local_path),
        )
        if existing is not None:
            durable_values: tuple[str, str] = (
                str(_column(existing, "content_hash")),
                str(_column(existing, "local_path")),
            )
            if durable_values != canonical_values:
                raise SourceDiscoveryError("Evidence asset identity collision")
            return False
        _ = connection.execute(
            """
            INSERT INTO evidence_assets (
                asset_id, content_hash, local_path, metadata_json
            )
            VALUES (?, ?, ?, ?)
            """,
            (document.asset.asset_id, *canonical_values, "{}"),
        )
        return True

    @staticmethod
    def _persist_acquisition(
        connection: sqlite3.Connection,
        document: EvidenceDocument,
        *,
        source_item: SourceItem,
    ) -> bool:
        if document.asset.source_item_id != source_item.source_item_id:
            raise SourceDiscoveryError("Evidence document does not belong to its source item")
        retrieved_at: str = _utc_text(document.asset.retrieved_at)
        acquisition = connection.execute(
            """
            INSERT INTO evidence_asset_acquisitions (
                asset_id, source_item_id, content_version, retrieved_at,
                media_type, provenance_status
            )
            VALUES (?, ?, ?, ?, ?, 'resolved')
            ON CONFLICT DO NOTHING
            """,
            (
                document.asset.asset_id,
                source_item.source_item_id,
                source_item.content_version,
                retrieved_at,
                document.asset.media_type,
            ),
        )
        if acquisition.rowcount > 0:
            return True
        existing: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                """
                SELECT media_type
                FROM evidence_asset_acquisitions
                WHERE asset_id = ?
                  AND source_item_id = ?
                  AND content_version = ?
                  AND retrieved_at = ?
                  AND provenance_status = 'resolved'
                """,
                (
                    document.asset.asset_id,
                    source_item.source_item_id,
                    source_item.content_version,
                    retrieved_at,
                ),
            ).fetchone(),
        )
        if existing is None or str(_column(existing, "media_type")) != document.asset.media_type:
            raise SourceDiscoveryError("Evidence acquisition identity collision")
        return False

    @staticmethod
    def _persist_fragment(
        connection: sqlite3.Connection,
        fragment: EvidenceFragment,
    ) -> bool:
        locator_json: str = json.dumps(
            fragment.locator.model_dump(mode="json"),
            sort_keys=True,
        )
        values: tuple[object, ...] = (
            fragment.asset_id,
            fragment.kind,
            locator_json,
            fragment.extracted_text,
            fragment.cited_source_text,
            fragment.extraction_method,
            fragment.extraction_model,
            fragment.confidence,
        )
        existing: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                """
                SELECT asset_id, fragment_kind, locator_json, extracted_text,
                       cited_source_text, extraction_method, extraction_model,
                       confidence
                FROM evidence_fragments
                WHERE fragment_id = ?
                """,
                (fragment.fragment_id,),
            ).fetchone(),
        )
        if existing is not None:
            durable_values: tuple[object, ...] = tuple(
                _column(existing, name)
                for name in (
                    "asset_id",
                    "fragment_kind",
                    "locator_json",
                    "extracted_text",
                    "cited_source_text",
                    "extraction_method",
                    "extraction_model",
                    "confidence",
                )
            )
            if durable_values != values:
                raise SourceDiscoveryError("Evidence fragment identity collision")
            return False
        _ = connection.execute(
            """
            INSERT INTO evidence_fragments (
                fragment_id, asset_id, fragment_kind, locator_json,
                extracted_text, cited_source_text, extraction_method,
                extraction_model, confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fragment.fragment_id, *values),
        )
        _ = connection.execute(
            """
            INSERT INTO evidence_fragment_search (
                fragment_id, extracted_text, cited_source_text
            )
            VALUES (?, ?, ?)
            """,
            (
                fragment.fragment_id,
                fragment.extracted_text,
                fragment.cited_source_text,
            ),
        )
        return True


class SourceSyncService:
    """Synchronize configured sources into assets, evidence, and durable cursors."""

    def __init__(
        self,
        registry_document: SourceRegistryDocument,
        adapters: AdapterRegistry,
        source_repository: SourceStateRepository,
        evidence_repository: EvidenceRepository,
        asset_store: AssetStore,
    ) -> None:
        """Bind a validated registry to its durable synchronization dependencies."""
        self._registry_document: SourceRegistryDocument = registry_document
        self._adapters: AdapterRegistry = adapters
        self._source_repository: SourceStateRepository = source_repository
        self._evidence_repository: EvidenceRepository = evidence_repository
        self._asset_store: AssetStore = asset_store

    def register_definitions(self) -> int:
        """Persist all configured definitions and return the changed count."""
        registered_at: datetime = utc_now()
        return sum(
            self._source_repository.register_definition(
                definition,
                registry_version=self._registry_document.version,
                registered_at=registered_at,
            )
            for definition in self._registry_document.sources
        )

    def sync(self, source_id: str) -> SourceSyncResult:
        """Synchronize one enabled source from its committed cursor."""
        definition: SourceDefinition = self._definition(source_id)
        cursor: SourceCursor | None = self._source_repository.get_cursor(
            source_id,
            purpose=SourceCursorPurpose.SYNC,
        )
        return self._sync_batch(definition, cursor)

    def backfill(self, source_id: str, *, maximum_batches: int) -> tuple[SourceSyncResult, ...]:
        """Resume a bounded historical scan from its independent cursor."""
        if maximum_batches <= 0:
            raise ValueError("maximum_batches must be positive")
        definition: SourceDefinition = self._definition(source_id)
        cursor: SourceCursor | None = self._source_repository.get_cursor(
            source_id,
            purpose=SourceCursorPurpose.BACKFILL,
        )
        results: list[SourceSyncResult] = []
        for _ in range(maximum_batches):
            result: SourceSyncResult = self._sync_batch(
                definition,
                cursor,
                cursor_purpose=SourceCursorPurpose.BACKFILL,
            )
            results.append(result)
            if not result.discovered_count or result.next_cursor == cursor:
                _ = self._source_repository.clear_cursor(
                    source_id,
                    purpose=SourceCursorPurpose.BACKFILL,
                )
                break
            cursor = result.next_cursor
            if cursor is None:
                _ = self._source_repository.clear_cursor(
                    source_id,
                    purpose=SourceCursorPurpose.BACKFILL,
                )
                break
        return tuple(results)

    def _definition(self, source_id: str) -> SourceDefinition:
        for definition in self._registry_document.sources:
            if definition.source_id == source_id:
                if not definition.enabled:
                    raise SourceDiscoveryError(f"Source is disabled: {source_id}")
                return definition
        raise SourceDiscoveryError(f"Source is not configured: {source_id}")

    def _sync_batch(
        self,
        definition: SourceDefinition,
        cursor: SourceCursor | None,
        *,
        cursor_purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> SourceSyncResult:
        connector = self._adapters.create(definition)
        batch = connector.discover(cursor)
        records: list[SourceIngestionRecord] = []
        for discovered_item in batch.items:
            artifact = connector.fetch(discovered_item)
            stored = self._asset_store.put_bytes(artifact.content)
            if stored.digest != artifact.content_hash:
                raise SourceDiscoveryError("Fetched artifact hash changed during persistence")
            extracted: EvidenceDocument = connector.extract(artifact)
            durable_document: EvidenceDocument = extracted.model_copy(
                update={
                    "asset": extracted.asset.model_copy(
                        update={"local_path": stored.path},
                    ),
                },
            )
            records.append(
                SourceIngestionRecord(
                    source_item=artifact.source_item,
                    evidence_document=durable_document,
                )
            )
        persistence: SourceIngestionPersistenceResult = self._evidence_repository.persist_ingestion_batch(
            definition.source_id,
            tuple(records),
            next_cursor=batch.next_cursor,
            updated_at=utc_now(),
            cursor_purpose=cursor_purpose,
        )
        return SourceSyncResult(
            source_id=definition.source_id,
            discovered_count=len(batch.items),
            persisted_count=persistence.persisted_item_count,
            evidence_document_count=persistence.changed_evidence_document_count,
            next_cursor=batch.next_cursor,
        )


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Source ingestion timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _optional_utc_text(value: datetime | None) -> str | None:
    return _utc_text(value) if value is not None else None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceDiscoveryError("Stored source-item timestamp is malformed")
    return value


def default_sources_path() -> Path:
    """Return the repository-local registry path used by CLI workflows."""
    return Path("sources.toml")
