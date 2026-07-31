from datetime import UTC
from datetime import datetime

from pytest import MonkeyPatch

from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.sources.http import HttpResponse
from money_pit.sources.http import WebConnector
from money_pit.sources.local import local_text_connector
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.service import EvidenceRepository
from money_pit.sources.service import SourceIngestionPersistenceResult
from money_pit.sources.service import SourceSyncResult
from money_pit.sources.service import SourceSyncService
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.sources import SourceRepository


NOW = datetime(2026, 7, 29, tzinfo=UTC)


class _StaticTransport:
    def get(self, url, *, maximum_bytes, timeout_seconds):
        del url, maximum_bytes, timeout_seconds
        return HttpResponse(
            b"plain claim",
            "text/plain",
            "https://example.com/claim.txt",
        )


def _service(tmp_path, definition):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    return SourceSyncService(
        SourceRegistryDocument(version=1, sources=(definition,)),
        AdapterRegistry(),
        SourceRepository(database),
        EvidenceRepository(database),
        AssetStore(tmp_path / "assets"),
    )


def test_web_connector_extracts_plain_text_without_html_parsing():
    connector = WebConnector(
        SourceDefinition(
            source_id="web",
            adapter_name="web",
            locator="https://example.com/claim.txt",
        ),
        _StaticTransport(),
    )

    artifact = connector.fetch(connector.discover(None).items[0])

    assert connector.extract(artifact).fragments[0].extracted_text == "plain claim"


def test_backfill_stops_after_later_page_returns_no_cursor(
    tmp_path,
    monkeypatch: MonkeyPatch,
):
    definition = SourceDefinition(
        source_id="source",
        adapter_name="unused",
        locator="unused",
    )
    service = _service(tmp_path, definition)
    results = iter(
        (
            SourceSyncResult(
                source_id="source",
                discovered_count=1,
                persisted_count=1,
                evidence_document_count=1,
                next_cursor=SourceCursor(value="next"),
            ),
            SourceSyncResult(
                source_id="source",
                discovered_count=1,
                persisted_count=1,
                evidence_document_count=1,
                next_cursor=None,
            ),
        ),
    )
    monkeypatch.setattr(service, "_sync_batch", lambda _definition, _cursor, **_kwargs: next(results))

    actual = service.backfill("source", maximum_batches=3)

    assert len(actual) == 2


def test_backfill_returns_when_batch_limit_is_exhausted(
    tmp_path,
    monkeypatch: MonkeyPatch,
):
    definition = SourceDefinition(
        source_id="source",
        adapter_name="unused",
        locator="unused",
    )
    service = _service(tmp_path, definition)
    cursors = iter((SourceCursor(value="one"), SourceCursor(value="two")))

    def next_result(_definition, _cursor, **_kwargs):
        return SourceSyncResult(
            source_id="source",
            discovered_count=1,
            persisted_count=1,
            evidence_document_count=1,
            next_cursor=next(cursors),
        )

    monkeypatch.setattr(service, "_sync_batch", next_result)

    actual = service.backfill("source", maximum_batches=2)

    assert len(actual) == 2


def test_sync_counts_only_new_evidence_documents(
    tmp_path,
    monkeypatch: MonkeyPatch,
):
    source = tmp_path / "source.txt"
    source.write_text("claim", encoding="utf-8")
    definition = SourceDefinition(
        source_id="local",
        adapter_name="local_text",
        locator=str(source),
    )
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    adapters = AdapterRegistry()
    adapters.register("local_text", local_text_connector)
    evidence_repository = EvidenceRepository(database)
    monkeypatch.setattr(
        evidence_repository,
        "persist_ingestion_batch",
        lambda _source_id, _records, **_kwargs: SourceIngestionPersistenceResult(
            persisted_item_count=1,
            changed_evidence_document_count=0,
        ),
    )
    service = SourceSyncService(
        SourceRegistryDocument(version=1, sources=(definition,)),
        adapters,
        SourceRepository(database),
        evidence_repository,
        AssetStore(tmp_path / "assets"),
    )
    service.register_definitions()

    result = service.sync("local")

    assert result.evidence_document_count == 0


def test_backfill_resumes_without_changing_sync_cursor(
    tmp_path,
    monkeypatch: MonkeyPatch,
):
    definition = SourceDefinition(
        source_id="source",
        adapter_name="unused",
        locator="unused",
    )
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    repository = SourceRepository(database)
    repository.persist_discovery(
        "source",
        (),
        next_cursor=SourceCursor(value="live"),
        updated_at=NOW,
    )
    repository.persist_discovery(
        "source",
        (),
        next_cursor=SourceCursor(value="historical-page-2"),
        updated_at=NOW,
        cursor_purpose=SourceCursorPurpose.BACKFILL,
    )
    service = SourceSyncService(
        SourceRegistryDocument(version=1, sources=(definition,)),
        AdapterRegistry(),
        repository,
        EvidenceRepository(database),
        AssetStore(tmp_path / "assets"),
    )
    observed_cursors = []

    def terminal_result(_definition, cursor, **_kwargs):
        observed_cursors.append(cursor)
        return SourceSyncResult(
            source_id="source",
            discovered_count=1,
            persisted_count=0,
            evidence_document_count=0,
            next_cursor=None,
        )

    monkeypatch.setattr(service, "_sync_batch", terminal_result)

    service.backfill("source", maximum_batches=1)

    assert observed_cursors == [SourceCursor(value="historical-page-2")]
    assert repository.get_cursor("source") == SourceCursor(value="live")
    assert (
        repository.get_cursor(
            "source",
            purpose=SourceCursorPurpose.BACKFILL,
        )
        is None
    )
