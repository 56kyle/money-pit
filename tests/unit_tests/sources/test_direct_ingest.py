import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from datetime import datetime
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING
from typing import cast

import pytest
from pydantic import SecretStr
from typing_extensions import override

from money_pit.evidence.media import MediaAnalysis
from money_pit.evidence.media import MediaEvidenceProcessor
from money_pit.evidence.media import TimedTranscriptSegment
from money_pit.evidence.processors import EvidenceProcessorRegistry
from money_pit.evidence.repository import EvidenceProcessingAttemptRepository
from money_pit.progress import IngestionProgressEvent
from money_pit.progress import IngestionProgressStage
from money_pit.progress import ignore_ingestion_progress
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceProcessingAttempt
from money_pit.schemas.evidence import EvidenceProcessingStatus
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.sources._shared import evidence_asset
from money_pit.sources._shared import source_definition_hash
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.errors import UnsupportedDirectIngestionError
from money_pit.sources.http import HttpResponse
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.service import EvidenceRepository
from money_pit.sources.service import SourceIngestionPersistenceResult
from money_pit.sources.service import SourceIngestionRecord
from money_pit.sources.service import SourceSyncService
from money_pit.sources.youtube import YouTubeConnector
from money_pit.sources.youtube import YouTubeMediaAcquisition
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.errors import AssetIntegrityError
from money_pit.storage.sources import SourceRepository


if TYPE_CHECKING:
    import sqlite3


_NOW = datetime(2026, 8, 10, tzinfo=UTC)
_API_PUBLISHED = datetime(2026, 8, 1, tzinfo=UTC)
_FIRST_MEDIA_PUBLISHED = datetime(2026, 8, 2, tzinfo=UTC)
_SECOND_MEDIA_PUBLISHED = datetime(2026, 8, 3, tzinfo=UTC)
_VIDEO = b"direct-video-content"
_ATTEMPT_COLLISION = "Evidence processing attempt identity collision"


class _UnusedHttpTransport:
    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        raise AssertionError((url, maximum_bytes, timeout_seconds))


class _MediaTransport:
    def __init__(self, *, published_at: datetime = _NOW) -> None:
        self.calls: list[tuple[str, int, float]] = []
        self.published_at: datetime = published_at

    def fetch(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> YouTubeMediaAcquisition:
        self.calls.append((url, maximum_bytes, timeout_seconds))
        return YouTubeMediaAcquisition(
            content=_VIDEO,
            media_type="video/mp4",
            filename=Path("video.mp4"),
            video_id="dQw4w9WgXcQ",
            channel_id="UCAAAAAAAAAAAAAAAAAAAAAA",
            published_at=self.published_at,
        )


class _PlaylistHttpTransport:
    def __init__(self, *, published_at: datetime) -> None:
        self._published_at: datetime = published_at

    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        del url, maximum_bytes, timeout_seconds
        payload = {
            "items": [
                {
                    "id": "playlist-item",
                    "snippet": {
                        "publishedAt": self._published_at.isoformat(),
                        "resourceId": {"videoId": "dQw4w9WgXcQ"},
                    },
                },
            ],
        }
        return HttpResponse(json.dumps(payload).encode(), "application/json", "https://example.test")


class _MediaAnalyzer:
    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        assert path.read_bytes() == _VIDEO
        assert media_type == "video/mp4"
        return MediaAnalysis(
            transcript=(TimedTranscriptSegment(start_seconds=0, end_seconds=1, text="Direct transcript."),),
            frames=(),
        )


class _CursorTrackingSourceRepository(SourceRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.cursor_reads: list[tuple[str, SourceCursorPurpose]] = []
        self.cursor_clears: list[tuple[str, SourceCursorPurpose]] = []

    @override
    def get_cursor(
        self,
        source_id: str,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> SourceCursor | None:
        self.cursor_reads.append((source_id, purpose))
        return super().get_cursor(source_id, purpose=purpose)

    @override
    def clear_cursor(self, source_id: str, *, purpose: SourceCursorPurpose) -> bool:
        self.cursor_clears.append((source_id, purpose))
        return super().clear_cursor(source_id, purpose=purpose)


class _DiscoveryOnlyConnector:
    def discover(
        self,
        cursor: SourceCursor | None,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> DiscoveryBatch:
        del cursor, purpose
        return DiscoveryBatch(items=(), discovered_at=_NOW)

    def fetch(self, item: SourceItem) -> RawArtifact:
        raise AssertionError(item)

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        raise AssertionError(artifact)


def _definition(*, enabled: bool = True, adapter_name: str = "youtube") -> SourceDefinition:
    return SourceDefinition(
        source_id="youtube",
        adapter_name=adapter_name,
        locator="UUAAAAAAAAAAAAAAAAAAAAAA",
        enabled=enabled,
        provenance_group="channel",
        allowed_uses=(AllowedUse.INTERPRETATION,),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.COMMENTARY),),
        adapter_config={"max_media_bytes": 1_024, "timeout_seconds": 7},
    )


def _service(
    tmp_path: Path,
    definition: SourceDefinition,
    adapters: AdapterRegistry,
    *,
    progress: list[IngestionProgressEvent] | None = None,
) -> tuple[SourceSyncService, Database, _CursorTrackingSourceRepository]:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    repository = _CursorTrackingSourceRepository(database)
    processors = EvidenceProcessorRegistry()
    processors.register(MediaEvidenceProcessor(_MediaAnalyzer()))
    service = SourceSyncService(
        SourceRegistryDocument(version="0.0.2", sources=(definition,)),
        adapters,
        repository,
        EvidenceRepository(database),
        AssetStore(tmp_path / "assets"),
        attempt_repository=EvidenceProcessingAttemptRepository(database),
        processor_registry=processors,
        progress=progress.append if progress is not None else ignore_ingestion_progress,
    )
    _ = service.register_definitions()
    return service, database, repository


def _direct_runtime(
    tmp_path: Path,
    *,
    media_transport: _MediaTransport | None = None,
    playlist_transport: _PlaylistHttpTransport | None = None,
    progress: list[IngestionProgressEvent] | None = None,
) -> tuple[
    SourceSyncService,
    Database,
    _CursorTrackingSourceRepository,
    _MediaTransport,
    list[str],
]:
    definition = _definition()
    credential_calls: list[str] = []
    selected_media_transport = media_transport or _MediaTransport()

    def discovery_api_key() -> SecretStr:
        credential_calls.append("discovery")
        if playlist_transport is None:
            raise AssertionError("direct ingestion must not resolve discovery credentials")
        return SecretStr("playlist-key")

    adapters = AdapterRegistry()
    adapters.register(
        "youtube",
        lambda configured: YouTubeConnector(
            configured,
            discovery_api_key,
            _MediaAnalyzer(),
            playlist_transport or _UnusedHttpTransport(),
            selected_media_transport,
        ),
    )
    service, database, repository = _service(tmp_path, definition, adapters, progress=progress)
    return service, database, repository, selected_media_transport, credential_calls


def test_ingest_uses_direct_media_seam_without_discovery_credentials(tmp_path: Path) -> None:
    service, _, _, media_transport, credential_calls = _direct_runtime(tmp_path)

    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")

    assert (media_transport.calls, credential_calls) == (
        [("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 1_024, 7.0)],
        [],
    )


def test_ingest_reuses_a_verified_durable_raw_acquisition_without_media_transport(tmp_path: Path) -> None:
    service, _, _, media_transport, _ = _direct_runtime(tmp_path)

    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")
    _ = service.ingest("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert media_transport.calls == [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 1_024, 7.0),
    ]


def test_ingest_with_refresh_forces_media_transport_after_a_cacheable_acquisition(tmp_path: Path) -> None:
    service, _, _, media_transport, _ = _direct_runtime(tmp_path)

    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")
    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ", refresh=True)

    assert len(media_transport.calls) == 2


def test_ingest_reacquires_and_persists_unchanged_media_for_a_new_source_definition(tmp_path: Path) -> None:
    service, database, _, media_transport, _ = _direct_runtime(tmp_path)
    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")
    changed_definition = _definition().model_copy(
        update={"adapter_config": {"max_media_bytes": 2_048, "timeout_seconds": 7}},
    )
    changed_adapters = AdapterRegistry()
    changed_adapters.register(
        "youtube",
        lambda configured: YouTubeConnector(
            configured,
            lambda: SecretStr("unused"),
            _MediaAnalyzer(),
            _UnusedHttpTransport(),
            media_transport,
        ),
    )
    processors = EvidenceProcessorRegistry()
    processors.register(MediaEvidenceProcessor(_MediaAnalyzer()))
    changed_service = SourceSyncService(
        SourceRegistryDocument(version="0.0.3", sources=(changed_definition,)),
        changed_adapters,
        SourceRepository(database),
        EvidenceRepository(database),
        AssetStore(tmp_path / "assets"),
        attempt_repository=EvidenceProcessingAttemptRepository(database),
        processor_registry=processors,
    )
    _ = changed_service.register_definitions()

    _ = changed_service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")
    _ = changed_service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")
    with database.transaction() as connection:
        rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                "SELECT DISTINCT source_definition_hash FROM evidence_asset_acquisitions",
            ).fetchall(),
        )
        persisted_definition_hashes = {str(cast("object", row["source_definition_hash"])) for row in rows}

    assert len(media_transport.calls) == 2
    assert persisted_definition_hashes == {
        source_definition_hash(_definition()),
        source_definition_hash(changed_definition),
    }


def test_ingest_refetches_when_the_cached_asset_file_is_missing(tmp_path: Path) -> None:
    service, _, _, media_transport, _ = _direct_runtime(tmp_path)
    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")
    digest = hashlib.sha256(_VIDEO).hexdigest()
    AssetStore(tmp_path / "assets").path_for_digest(digest).unlink()

    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")

    assert len(media_transport.calls) == 2


@pytest.mark.parametrize(
    "replacement",
    [
        pytest.param(b"corrupt-video-content", id="corrupt"),
        pytest.param(b"x" * 1_025, id="oversized"),
    ],
)
def test_ingest_rejects_an_invalid_cached_asset_without_refetching(
    tmp_path: Path,
    replacement: bytes,
) -> None:
    service, _, _, media_transport, _ = _direct_runtime(tmp_path)
    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")
    digest = hashlib.sha256(_VIDEO).hexdigest()
    _ = AssetStore(tmp_path / "assets").path_for_digest(digest).write_bytes(replacement)

    with pytest.raises(AssetIntegrityError):
        _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")

    assert len(media_transport.calls) == 1


def test_ingest_emits_typed_progress_for_cache_and_processing_stages(tmp_path: Path) -> None:
    progress: list[IngestionProgressEvent] = []
    service, _, _, _, _ = _direct_runtime(tmp_path, progress=progress)

    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")

    assert all(isinstance(event, IngestionProgressEvent) for event in progress)
    assert tuple(event.stage for event in progress) == (
        IngestionProgressStage.CACHE_MISS,
        IngestionProgressStage.DOWNLOAD_STARTED,
        IngestionProgressStage.DOWNLOAD_COMPLETED,
        IngestionProgressStage.PERSISTENCE,
        IngestionProgressStage.PERSISTENCE,
        IngestionProgressStage.PERSISTENCE,
        IngestionProgressStage.COMPLETED,
    )


def test_ingest_reports_one_requested_ingestion_and_changed_evidence(tmp_path: Path) -> None:
    service, _, _, _, _ = _direct_runtime(tmp_path)

    result = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")

    assert (
        result.source_id,
        result.source_item_id,
        result.ingested_count,
        result.evidence_document_count,
    ) == ("youtube", "youtube:dQw4w9WgXcQ", 1, 1)


def test_ingest_persists_evidence_and_its_successful_processing_attempt(tmp_path: Path) -> None:
    service, database, _, _, _ = _direct_runtime(tmp_path)

    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")

    with database.transaction() as connection:
        evidence_count = cast("int", connection.execute("SELECT COUNT(*) FROM evidence_assets").fetchone()[0])
        attempts = cast(
            "list[sqlite3.Row]",
            connection.execute(
                "SELECT status, processor_name FROM evidence_processing_attempts",
            ).fetchall(),
        )
    assert (evidence_count, [(row["status"], row["processor_name"]) for row in attempts]) == (
        1,
        [("succeeded", "timestamped-media")],
    )


def test_ingest_neither_reads_nor_changes_discovery_cursors(tmp_path: Path) -> None:
    service, database, repository, _, _ = _direct_runtime(tmp_path)
    definition = _definition()
    sync_cursor = SourceCursor(value="sync-cursor")
    backfill_cursor = SourceCursor(value="backfill-cursor")
    _ = repository.persist_discovery(
        definition.source_id,
        (),
        next_cursor=sync_cursor,
        updated_at=_NOW,
        cursor_purpose=SourceCursorPurpose.SYNC,
    )
    _ = repository.persist_discovery(
        definition.source_id,
        (),
        next_cursor=backfill_cursor,
        updated_at=_NOW,
        cursor_purpose=SourceCursorPurpose.BACKFILL,
    )

    _ = service.ingest(definition.source_id, "https://youtu.be/dQw4w9WgXcQ")

    persisted_cursors = (
        SourceRepository(database).get_cursor(definition.source_id, purpose=SourceCursorPurpose.SYNC),
        SourceRepository(database).get_cursor(definition.source_id, purpose=SourceCursorPurpose.BACKFILL),
    )
    assert (repository.cursor_reads, repository.cursor_clears, persisted_cursors) == (
        [],
        [],
        (sync_cursor, backfill_cursor),
    )


def _durable_temporal_values(database: Database) -> tuple[object, object, object]:
    with database.transaction() as connection:
        row = cast(
            "sqlite3.Row",
            connection.execute(
                "SELECT published_at, updated_at, discovered_at FROM source_items",
            ).fetchone(),
        )
    return row["published_at"], row["updated_at"], row["discovered_at"]


def test_ingest_exact_retry_is_idempotent_and_preserves_the_original_discovery_time(tmp_path: Path) -> None:
    service, database, _, _, _ = _direct_runtime(tmp_path)

    first_result = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")
    first_temporal_values = _durable_temporal_values(database)
    second_result = service.ingest("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    second_temporal_values = _durable_temporal_values(database)

    with database.transaction() as connection:
        durable_counts = (
            cast("int", connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0]),
            cast("int", connection.execute("SELECT COUNT(*) FROM evidence_assets").fetchone()[0]),
            cast("int", connection.execute("SELECT COUNT(*) FROM evidence_fragments").fetchone()[0]),
        )
    assert (
        first_result.ingested_count,
        second_result.ingested_count,
        second_result.evidence_document_count,
        first_temporal_values,
        second_temporal_values,
        durable_counts,
    ) == (1, 1, 0, first_temporal_values, first_temporal_values, (1, 1, 1))


def test_playlist_sync_then_direct_ingest_reuses_the_durable_media_timestamps(tmp_path: Path) -> None:
    media_transport = _MediaTransport(published_at=_FIRST_MEDIA_PUBLISHED)
    service, database, _, _, _ = _direct_runtime(
        tmp_path,
        media_transport=media_transport,
        playlist_transport=_PlaylistHttpTransport(published_at=_API_PUBLISHED),
    )

    _ = service.sync("youtube")
    sync_temporal_values = _durable_temporal_values(database)
    media_transport.published_at = _SECOND_MEDIA_PUBLISHED
    direct_result = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")

    assert (
        sync_temporal_values[0],
        _durable_temporal_values(database),
        direct_result.ingested_count,
    ) == (
        _FIRST_MEDIA_PUBLISHED.isoformat(),
        sync_temporal_values,
        1,
    )


def test_direct_ingest_then_playlist_sync_reuses_the_durable_media_timestamps(tmp_path: Path) -> None:
    media_transport = _MediaTransport(published_at=_FIRST_MEDIA_PUBLISHED)
    service, database, _, _, _ = _direct_runtime(
        tmp_path,
        media_transport=media_transport,
        playlist_transport=_PlaylistHttpTransport(published_at=_API_PUBLISHED),
    )

    _ = service.ingest("youtube", "https://youtu.be/dQw4w9WgXcQ")
    direct_temporal_values = _durable_temporal_values(database)
    media_transport.published_at = _SECOND_MEDIA_PUBLISHED
    sync_result = service.sync("youtube")

    assert (
        direct_temporal_values[0],
        _durable_temporal_values(database),
        sync_result.persisted_count,
    ) == (
        _FIRST_MEDIA_PUBLISHED.isoformat(),
        direct_temporal_values,
        0,
    )


def test_persist_ingestion_batch_rolls_back_evidence_when_attempt_admission_fails(tmp_path: Path) -> None:
    definition = _definition()

    def connector_factory(configured: SourceDefinition) -> _DiscoveryOnlyConnector:
        del configured
        return _DiscoveryOnlyConnector()

    adapters = AdapterRegistry()
    adapters.register("youtube", connector_factory)
    _, database, _ = _service(tmp_path, definition, adapters)
    connector = YouTubeConnector(
        definition,
        lambda: SecretStr("unused"),
        _MediaAnalyzer(),
        _UnusedHttpTransport(),
        _MediaTransport(),
    )
    item = connector.source_item_from_url("https://youtu.be/dQw4w9WgXcQ")
    artifact = connector.fetch(item)
    record = SourceIngestionRecord(
        source_item=artifact.source_item,
        evidence_document=EvidenceDocument(asset=evidence_asset(artifact), fragments=()),
    )
    attempt = EvidenceProcessingAttempt(
        attempt_id="colliding-attempt",
        source_item_id=artifact.source_item.source_item_id,
        content_version=artifact.source_item.content_version,
        asset_id=artifact.content_hash,
        processor_name="processor-one",
        processor_version="1",
        started_at=_NOW,
        completed_at=_NOW,
        status=EvidenceProcessingStatus.SUCCEEDED,
        document_id=artifact.content_hash,
    )
    conflicting_attempt = attempt.model_copy(update={"processor_name": "processor-two"})

    with pytest.raises(ValueError, match=_ATTEMPT_COLLISION):
        _ = EvidenceRepository(database).persist_ingestion_batch(
            definition.source_id,
            (record,),
            next_cursor=None,
            updated_at=_NOW,
            attempts=(attempt, conflicting_attempt),
        )

    with database.transaction() as connection:
        durable_counts = (
            cast("int", connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0]),
            cast("int", connection.execute("SELECT COUNT(*) FROM evidence_assets").fetchone()[0]),
            cast("int", connection.execute("SELECT COUNT(*) FROM evidence_processing_attempts").fetchone()[0]),
        )
    assert durable_counts == (0, 0, 0)


@pytest.mark.parametrize(
    ("field_name", "conflicting_value"),
    [
        pytest.param("source_id", "other-source", id="source-id"),
        pytest.param("source_definition_hash", "f" * 64, id="definition-hash"),
        pytest.param("canonical_uri", "https://www.youtube.com/watch?v=AAAAAAAAAAA", id="canonical-uri"),
    ],
)
def test_persist_ingestion_batch_rejects_an_invariant_source_item_identity_collision(
    tmp_path: Path,
    field_name: str,
    conflicting_value: str,
) -> None:
    definition = _definition()

    def connector_factory(configured: SourceDefinition) -> _DiscoveryOnlyConnector:
        del configured
        return _DiscoveryOnlyConnector()

    adapters = AdapterRegistry()
    adapters.register("youtube", connector_factory)
    _, database, _ = _service(tmp_path, definition, adapters)
    connector = YouTubeConnector(
        definition,
        lambda: SecretStr("unused"),
        _MediaAnalyzer(),
        _UnusedHttpTransport(),
        _MediaTransport(),
    )
    artifact = connector.fetch(connector.source_item_from_url("https://youtu.be/dQw4w9WgXcQ"))
    document = EvidenceDocument(asset=evidence_asset(artifact), fragments=())
    repository = EvidenceRepository(database)
    _ = repository.persist_ingestion_batch(
        definition.source_id,
        (SourceIngestionRecord(source_item=artifact.source_item, evidence_document=document),),
        next_cursor=None,
        updated_at=_NOW,
    )
    conflicting_item = artifact.source_item.model_copy(update={field_name: conflicting_value})

    with pytest.raises(SourceDiscoveryError):
        _ = repository.persist_ingestion_batch(
            definition.source_id,
            (SourceIngestionRecord(source_item=conflicting_item, evidence_document=document),),
            next_cursor=None,
            updated_at=_NOW,
        )


def test_concurrent_ingestion_transactions_return_one_durable_temporal_identity(tmp_path: Path) -> None:
    definition = _definition()

    def connector_factory(configured: SourceDefinition) -> _DiscoveryOnlyConnector:
        del configured
        return _DiscoveryOnlyConnector()

    adapters = AdapterRegistry()
    adapters.register("youtube", connector_factory)
    _, database, _ = _service(tmp_path, definition, adapters)
    connector = YouTubeConnector(
        definition,
        lambda: SecretStr("unused"),
        _MediaAnalyzer(),
        _UnusedHttpTransport(),
        _MediaTransport(),
    )
    artifact = connector.fetch(connector.source_item_from_url("https://youtu.be/dQw4w9WgXcQ"))
    first_item = artifact.source_item.model_copy(
        update={
            "published_at": _FIRST_MEDIA_PUBLISHED,
            "updated_at": _FIRST_MEDIA_PUBLISHED,
            "discovered_at": _FIRST_MEDIA_PUBLISHED,
        },
    )
    second_item = artifact.source_item.model_copy(
        update={
            "published_at": _SECOND_MEDIA_PUBLISHED,
            "updated_at": _SECOND_MEDIA_PUBLISHED,
            "discovered_at": _SECOND_MEDIA_PUBLISHED,
        },
    )
    document = EvidenceDocument(asset=evidence_asset(artifact), fragments=())
    records = (
        SourceIngestionRecord(source_item=first_item, evidence_document=document),
        SourceIngestionRecord(source_item=second_item, evidence_document=document),
    )
    ready = Barrier(2, timeout=5)

    def persist(record: SourceIngestionRecord) -> SourceIngestionPersistenceResult:
        _ = ready.wait()
        return EvidenceRepository(database).persist_ingestion_batch(
            definition.source_id,
            (record,),
            next_cursor=None,
            updated_at=_NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(persist, record) for record in records)
        results = tuple(future.result(timeout=10) for future in futures)

    durable_temporal_values = _durable_temporal_values(database)
    normalized_temporal_values = tuple(
        (
            result.normalized_source_items[0].published_at,
            result.normalized_source_items[0].updated_at,
            result.normalized_source_items[0].discovered_at,
        )
        for result in results
    )
    expected_temporal_values = tuple(datetime.fromisoformat(cast("str", value)) for value in durable_temporal_values)
    assert (
        {result.persisted_item_count for result in results},
        normalized_temporal_values,
    ) == (
        {0, 1},
        (expected_temporal_values, expected_temporal_values),
    )


def test_ingest_rejects_a_connector_without_direct_url_capability(tmp_path: Path) -> None:
    definition = _definition(adapter_name="discovery-only")

    def connector_factory(configured: SourceDefinition) -> _DiscoveryOnlyConnector:
        del configured
        return _DiscoveryOnlyConnector()

    adapters = AdapterRegistry()
    adapters.register("discovery-only", connector_factory)
    service, _, _ = _service(tmp_path, definition, adapters)

    with pytest.raises(UnsupportedDirectIngestionError):
        _ = service.ingest(definition.source_id, "https://youtu.be/dQw4w9WgXcQ")


def test_ingest_rejects_a_disabled_source_before_constructing_its_connector(tmp_path: Path) -> None:
    definition = _definition(enabled=False)
    connector_constructions = 0

    def connector_factory(configured: SourceDefinition) -> _DiscoveryOnlyConnector:
        nonlocal connector_constructions
        del configured
        connector_constructions += 1
        return _DiscoveryOnlyConnector()

    adapters = AdapterRegistry()
    adapters.register("youtube", connector_factory)
    service, _, _ = _service(tmp_path, definition, adapters)

    with pytest.raises(SourceDiscoveryError):
        _ = service.ingest(definition.source_id, "https://youtu.be/dQw4w9WgXcQ")

    assert connector_constructions == 0
