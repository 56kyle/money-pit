import hashlib
import json
from dataclasses import dataclass

import pytest

from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.sources.builtin import builtin_adapter_registry
from money_pit.sources.errors import ConnectorConfigurationError
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.errors import SourceMediaTypeError
from money_pit.sources.http import HttpResponse
from money_pit.sources.http import WebConnector
from money_pit.sources.http import validate_public_http_url
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.sec import SecFilingsConnector
from money_pit.sources.service import EvidenceRepository
from money_pit.sources.service import SourceSyncService
from money_pit.sources.youtube import YouTubeConnector
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.sources import SourceRepository


@dataclass
class StaticTransport:
    response: HttpResponse
    requested_url: str | None = None

    def get(self, url, *, maximum_bytes, timeout_seconds):
        del maximum_bytes, timeout_seconds
        self.requested_url = url
        return self.response


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "http://localhost/private",
        "http://127.0.0.1/private",
        "https://user:password@example.com/private",
    ],
)
def test_validate_public_http_url_rejects_unsafe_location(url):
    with pytest.raises(SourceDiscoveryError):
        validate_public_http_url(url)


def test_web_connector_fetch_and_extract_retains_visible_text():
    definition = SourceDefinition(source_id="web", adapter_name="web", locator="https://example.com")
    transport = StaticTransport(
        HttpResponse(
            b"<html><style>hidden</style><p>Visible claim</p><script>hidden</script></html>",
            "text/html",
            "https://example.com/final",
        )
    )
    connector = WebConnector(definition, transport)

    artifact = connector.fetch(connector.discover(None).items[0])
    document = connector.extract(artifact)

    assert artifact.source_item.content_version == artifact.content_hash
    assert artifact.canonical_uri == "https://example.com/final"
    assert document.fragments[0].extracted_text == "Visible claim"


def test_web_connector_rejects_wrong_mime():
    definition = SourceDefinition(source_id="web", adapter_name="web", locator="https://example.com")
    connector = WebConnector(
        definition,
        StaticTransport(HttpResponse(b"{}", "application/json", "https://example.com")),
    )

    with pytest.raises(SourceMediaTypeError):
        connector.fetch(connector.discover(None).items[0])


def test_youtube_discovery_uses_cursor_and_parses_items(monkeypatch):
    monkeypatch.setenv("YOUTUBE_KEY", "secret")
    response = {
        "nextPageToken": "next",
        "items": [
            {
                "id": "playlist-item",
                "snippet": {
                    "publishedAt": "2026-07-29T12:00:00Z",
                    "resourceId": {"videoId": "video"},
                },
            }
        ],
    }
    transport = StaticTransport(HttpResponse(json.dumps(response).encode(), "application/json", "https://example.com"))
    connector = YouTubeConnector(
        SourceDefinition(
            source_id="youtube",
            adapter_name="youtube",
            locator="uploads",
            adapter_config={"api_key_env": "YOUTUBE_KEY"},
        ),
        transport,
    )

    batch = connector.discover(None)

    assert batch.items[0].canonical_uri == "https://www.youtube.com/watch?v=video"
    assert batch.next_cursor.value == "next"
    assert "key=secret" in transport.requested_url


def test_youtube_requires_configured_key(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    definition = SourceDefinition(
        source_id="youtube",
        adapter_name="youtube",
        locator="uploads",
        adapter_config={"api_key_env": "MISSING_KEY"},
    )

    with pytest.raises(ConnectorConfigurationError):
        YouTubeConnector(definition)


def test_sec_connector_rejects_non_sec_host():
    definition = SourceDefinition(
        source_id="sec",
        adapter_name="sec_filings",
        locator="https://example.com/feed",
    )

    with pytest.raises(ConnectorConfigurationError):
        SecFilingsConnector(definition)


def test_builtin_adapter_registry_contains_all_contract_adapters():
    registry = builtin_adapter_registry()

    assert all(
        registry.contains(name)
        for name in (
            "local_text",
            "local_audio",
            "local_pdf",
            "local_email",
            "manual_url",
            "web",
            "rss",
            "atom",
            "youtube",
            "sec_filings",
        )
    )


def test_source_sync_service_persists_assets_evidence_and_cursor(tmp_path):
    source_path = tmp_path / "source.txt"
    source_path.write_text("Durable evidence", encoding="utf-8")
    definition = SourceDefinition(
        source_id="local",
        adapter_name="local_text",
        locator=str(source_path),
    )
    document = SourceRegistryDocument(version=1, sources=(definition,))
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    source_repository = SourceRepository(database)
    adapters = AdapterRegistry()
    from money_pit.sources.local import local_text_connector

    adapters.register("local_text", local_text_connector)
    service = SourceSyncService(
        document,
        adapters,
        source_repository,
        EvidenceRepository(database),
        AssetStore(tmp_path / "assets"),
    )
    service.register_definitions()

    first = service.sync("local")
    second = service.sync("local")

    assert (first.discovered_count, first.persisted_count, first.evidence_document_count) == (1, 1, 1)
    assert (second.discovered_count, second.persisted_count, second.evidence_document_count) == (0, 0, 0)


def test_source_sync_service_rejects_disabled_and_unknown_sources(tmp_path):
    definition = SourceDefinition(
        source_id="disabled",
        adapter_name="local_text",
        locator=str(tmp_path / "missing.txt"),
        enabled=False,
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

    with pytest.raises(SourceDiscoveryError):
        service.sync("disabled")
    with pytest.raises(SourceDiscoveryError):
        service.sync("missing")


def test_source_sync_persists_fetched_web_content_version_for_acquisition(tmp_path):
    content = b"content whose hash replaces the provisional version"
    digest = hashlib.sha256(content).hexdigest()
    definition = SourceDefinition(
        source_id="web",
        adapter_name="web",
        locator="https://example.com/item",
    )
    transport = StaticTransport(
        HttpResponse(content, "text/plain", definition.locator),
    )
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    source_repository = SourceRepository(database)
    adapters = AdapterRegistry()
    adapters.register(
        "web",
        lambda registered_definition: WebConnector(registered_definition, transport),
    )
    service = SourceSyncService(
        SourceRegistryDocument(version=1, sources=(definition,)),
        adapters,
        source_repository,
        EvidenceRepository(database),
        AssetStore(tmp_path / "assets"),
    )
    service.register_definitions()

    service.sync(definition.source_id)

    with database.transaction() as connection:
        provenance = connection.execute(
            """
            SELECT source_items.content_version, acquisitions.content_version,
                   acquisitions.asset_id
            FROM evidence_asset_acquisitions AS acquisitions
            JOIN source_items
              ON source_items.source_item_id = acquisitions.source_item_id
             AND source_items.content_version = acquisitions.content_version
            """,
        ).fetchone()
    assert tuple(provenance) == (digest, digest, digest)
