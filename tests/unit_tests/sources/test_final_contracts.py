# pyright: reportPrivateUsage=false

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.sources import cli
from money_pit.sources._shared import read_bounded
from money_pit.sources.errors import SourceContentTooLargeError
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.feeds import FeedConnector
from money_pit.sources.feeds import _child_text
from money_pit.sources.feeds import _entry_link
from money_pit.sources.feeds import _parse_datetime
from money_pit.sources.feeds import _parse_feed_entries
from money_pit.sources.http import HttpResponse
from money_pit.sources.local import local_pdf_connector
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.registry import UnknownAdapterError
from money_pit.sources.registry import UnsupportedSourceRegistryVersionError
from money_pit.sources.registry import load_source_registry
from money_pit.sources.sec import SecFilingsConnector
from money_pit.sources.service import EvidenceRepository
from money_pit.sources.service import SourceSyncResult
from money_pit.sources.service import SourceSyncService
from money_pit.sources.youtube import YouTubeConnector
from money_pit.storage.assets import AssetStore
from money_pit.storage.assets import StoredAsset
from money_pit.storage.database import Database
from money_pit.storage.sources import SourceRepository


NOW = datetime(2026, 7, 29, tzinfo=UTC)


@dataclass
class MutableTransport:
    response: HttpResponse
    requested_url: str | None = None

    def get(self, url, *, maximum_bytes, timeout_seconds):
        del maximum_bytes, timeout_seconds
        self.requested_url = url
        return self.response


def _youtube_connector(monkeypatch: MonkeyPatch, transport: MutableTransport) -> YouTubeConnector:
    monkeypatch.setenv("YOUTUBE_KEY", "secret")
    return YouTubeConnector(
        SourceDefinition(
            source_id="youtube",
            adapter_name="youtube",
            locator="uploads",
            adapter_config={"api_key_env": "YOUTUBE_KEY"},
        ),
        transport,
    )


def test_read_bounded_wraps_read_failure(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("content", encoding="utf-8")

    def fail_read(_path: Path) -> bytes:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_bytes", fail_read)

    with pytest.raises(SourceContentTooLargeError):
        read_bounded(source, 100)


def test_read_bounded_rechecks_observed_size(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("x", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", lambda _path: b"too large")

    with pytest.raises(SourceContentTooLargeError):
        read_bounded(source, 1)


def test_feed_helpers_cover_missing_link_atom_link_and_dates():
    root = ET.fromstring(  # noqa: S314 - fixed trusted test data
        """
        <feed xmlns="urn:test">
          <entry><id>missing</id></entry>
          <entry><id>atom</id><link href="https://example.com/atom"/>
            <updated>2026-07-29T12:00:00Z</updated></entry>
        </feed>
        """
    )

    entries = _parse_feed_entries(root, "feed")

    assert tuple(item.source_item_id for item in entries) == ("feed:atom",)
    assert entries[0].published_at == NOW.replace(hour=12)


def test_feed_helpers_return_none_for_absent_values():
    element = ET.fromstring("<item><link/></item>")  # noqa: S314 - fixed trusted test data

    assert (_child_text(element, ("guid",)), _entry_link(element), _parse_datetime(None)) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize("value", ["not-a-date", "2026-07-29T12:00:00"])
def test__parse_datetime_handles_invalid_and_naive_values(value):
    parsed = _parse_datetime(value)

    assert parsed is None if value == "not-a-date" else parsed == NOW.replace(hour=12)


def test_feed_connector_delegates_fetch_and_extract():
    definition = SourceDefinition(
        source_id="feed",
        adapter_name="rss",
        locator="https://example.com/feed.xml",
    )
    transport = MutableTransport(
        HttpResponse(b"<p>claim</p>", "text/html", "https://example.com/item"),
    )
    connector = FeedConnector(definition, transport)
    item = SourceItem(
        source_item_id="feed:item",
        source_id="feed",
        canonical_uri="https://example.com/item",
        discovered_at=NOW,
        content_version="v1",
    )

    artifact = connector.fetch(item)
    document = connector.extract(artifact)

    assert document.fragments[0].extracted_text == "claim"


def test_adapter_registry_create_rejects_unknown_adapter():
    definition = SourceDefinition(
        source_id="source",
        adapter_name="missing",
        locator="https://example.com",
    )

    with pytest.raises(UnknownAdapterError):
        AdapterRegistry().create(definition)


def test_load_source_registry_rejects_unsupported_version(tmp_path):
    registry_path = tmp_path / "sources.toml"
    registry_path.write_text("version = 2\n", encoding="utf-8")

    with pytest.raises(UnsupportedSourceRegistryVersionError):
        load_source_registry(registry_path, AdapterRegistry())


def test_sec_connector_delegates_discovery_fetch_and_extract():
    definition = SourceDefinition(
        source_id="sec",
        adapter_name="sec_filings",
        locator="https://www.sec.gov/feed.xml",
    )
    transport = MutableTransport(
        HttpResponse(
            b"<rss><channel><item><guid>1</guid><link>https://www.sec.gov/filing</link></item></channel></rss>",
            "application/rss+xml",
            definition.locator,
        ),
    )
    connector = SecFilingsConnector(definition, transport)
    batch = connector.discover(None)
    transport.response = HttpResponse(
        b"<p>filing</p>",
        "text/html",
        "https://www.sec.gov/filing",
    )

    artifact = connector.fetch(batch.items[0])
    document = connector.extract(artifact)

    assert document.fragments[0].extracted_text == "filing"


def test_youtube_discovery_rejects_malformed_response(
    monkeypatch: MonkeyPatch,
):
    transport = MutableTransport(
        HttpResponse(b'{"items":[{"id":"missing-snippet"}]}', "application/json", "https://example.com"),
    )
    connector = _youtube_connector(monkeypatch, transport)

    with pytest.raises(SourceDiscoveryError, match="malformed"):
        connector.discover(None)


def test_youtube_cursor_and_web_delegation(monkeypatch: MonkeyPatch):
    transport = MutableTransport(
        HttpResponse(b'{"items":[]}', "application/json", "https://example.com"),
    )
    connector = _youtube_connector(monkeypatch, transport)

    batch = connector.discover(SourceCursor(value="page"))
    assert "pageToken=page" in str(transport.requested_url)
    assert batch.next_cursor is None

    transport.response = HttpResponse(
        b"<p>video claim</p>",
        "text/html",
        "https://www.youtube.com/watch?v=video",
    )
    item = SourceItem(
        source_item_id="youtube:item",
        source_id="youtube",
        canonical_uri="https://www.youtube.com/watch?v=video",
        discovered_at=NOW,
        content_version="item",
    )
    artifact = connector.fetch(item)

    assert connector.extract(artifact).fragments[0].extracted_text == "video claim"


def test_local_pdf_connector_retains_binary_without_inference(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7")
    connector = local_pdf_connector(
        SourceDefinition(
            source_id="pdf",
            adapter_name="local_pdf",
            locator=str(source),
        )
    )

    artifact = connector.fetch(connector.discover(None).items[0])

    assert connector.extract(artifact).fragments == ()


def test_source_runtime_composes_and_registers_durable_dependencies(
    tmp_path,
    monkeypatch: MonkeyPatch,
):
    source = tmp_path / "source.txt"
    source.write_text("claim", encoding="utf-8")
    sources_path = tmp_path / "sources.toml"
    sources_path.write_text(
        f'version = 1\n[[sources]]\nsource_id = "local"\nadapter_name = "local_text"\nlocator = "{source.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "DATA_ROOT", tmp_path / "data")

    _, repository = cli._source_runtime(sources_path)

    assert repository.get_definition("local") is not None


def test_source_sync_rejects_hash_change_during_persistence(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("claim", encoding="utf-8")
    definition = SourceDefinition(
        source_id="local",
        adapter_name="local_text",
        locator=str(source),
    )
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    repository = SourceRepository(database)
    adapters = AdapterRegistry()
    from money_pit.sources.local import local_text_connector

    adapters.register("local_text", local_text_connector)
    asset_store = AssetStore(tmp_path / "assets")
    monkeypatch.setattr(
        asset_store,
        "put_bytes",
        lambda _content: StoredAsset(
            digest="0" * 64,
            path=tmp_path / "asset",
            size_bytes=0,
        ),
    )
    service = SourceSyncService(
        SourceRegistryDocument(version=1, sources=(definition,)),
        adapters,
        repository,
        EvidenceRepository(database),
        asset_store,
    )

    with pytest.raises(SourceDiscoveryError, match="hash changed"):
        service.sync("local")


def test_backfill_rejects_nonpositive_batch_limit(tmp_path):
    service = SourceSyncService(
        SourceRegistryDocument(version=1),
        AdapterRegistry(),
        SourceRepository(Database(tmp_path / "unused.sqlite3")),
        EvidenceRepository(Database(tmp_path / "unused.sqlite3")),
        AssetStore(tmp_path / "assets"),
    )

    with pytest.raises(ValueError, match="maximum_batches must be positive"):
        service.backfill("source", maximum_batches=0)


@pytest.mark.parametrize(
    ("results", "expected_count"),
    [
        (
            (
                SourceSyncResult(
                    source_id="source",
                    discovered_count=0,
                    persisted_count=0,
                    evidence_document_count=0,
                    next_cursor=None,
                ),
            ),
            1,
        ),
        (
            (
                SourceSyncResult(
                    source_id="source",
                    discovered_count=1,
                    persisted_count=1,
                    evidence_document_count=1,
                    next_cursor=None,
                ),
            ),
            1,
        ),
    ],
)
def test_backfill_stops_when_no_more_pages(
    tmp_path,
    monkeypatch,
    results,
    expected_count,
):
    definition = SourceDefinition(
        source_id="source",
        adapter_name="unused",
        locator="unused",
    )
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    service = SourceSyncService(
        SourceRegistryDocument(version=1, sources=(definition,)),
        AdapterRegistry(),
        SourceRepository(database),
        EvidenceRepository(database),
        AssetStore(tmp_path / "assets"),
    )
    pending = list(results)
    monkeypatch.setattr(service, "_sync_batch", lambda _definition, _cursor, **_kwargs: pending.pop(0))

    actual = service.backfill("source", maximum_batches=3)

    assert len(actual) == expected_count
