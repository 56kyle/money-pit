from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.sources._shared import BoundedConnectorConfig
from money_pit.sources._shared import guessed_media_type
from money_pit.sources._shared import parse_config
from money_pit.sources._shared import read_bounded
from money_pit.sources._shared import require_media_type
from money_pit.sources.errors import ConnectorConfigurationError
from money_pit.sources.errors import SourceContentTooLargeError
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.service import EvidenceRepository
from money_pit.sources.service import SourceIngestionRecord
from money_pit.storage.database import Database
from money_pit.storage.sources import SourceRepository


NOW = datetime(2026, 7, 29, tzinfo=UTC)
DIGEST = "a" * 64


def _source_item(
    *,
    source_item_id: str = "source:item",
    source_id: str = "source",
    content_version: str = "version-1",
) -> SourceItem:
    return SourceItem(
        source_item_id=source_item_id,
        source_id=source_id,
        canonical_uri=f"https://example.com/{source_item_id}",
        discovered_at=NOW,
        content_version=content_version,
    )


def _document(*, media_type="text/plain", source_item_id: str = "source:item"):
    asset = EvidenceAsset(
        asset_id=DIGEST,
        content_hash=DIGEST,
        media_type=media_type,
        source_item_id=source_item_id,
        local_path=Path("aa") / DIGEST,
        retrieved_at=NOW,
    )
    fragment = EvidenceFragment(
        fragment_id="fragment",
        asset_id=DIGEST,
        kind="web_span",
        locator=TextLocator(start_offset=0, end_offset=4),
        extracted_text="text",
        extraction_method="test",
    )
    return EvidenceDocument(asset=asset, fragments=(fragment,))


def _persist_source_item(database: Database, item: SourceItem) -> None:
    source_repository = SourceRepository(database)
    source_repository.register_definition(
        SourceDefinition(
            source_id=item.source_id,
            adapter_name="test",
            locator=item.canonical_uri,
        ),
        registry_version=1,
        registered_at=NOW,
    )
    source_repository.persist_discovery(
        item.source_id,
        (item,),
        next_cursor=None,
        updated_at=NOW,
    )


def test_evidence_repository_persists_search_and_is_idempotent(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    repository = EvidenceRepository(database)
    item = _source_item()
    _persist_source_item(database, item)

    assert repository.persist(_document(), source_item=item) is True
    assert repository.persist(_document(), source_item=item) is False
    with database.transaction() as connection:
        match = connection.execute(
            "SELECT fragment_id FROM evidence_fragment_search WHERE evidence_fragment_search MATCH 'text'"
        ).fetchone()
    assert match["fragment_id"] == "fragment"


def test_evidence_repository_rejects_asset_identity_collision(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    repository = EvidenceRepository(database)
    item = _source_item()
    _persist_source_item(database, item)
    repository.persist(_document(), source_item=item)

    with pytest.raises(SourceDiscoveryError):
        repository.persist(_document(media_type="text/html"), source_item=item)


def test_parse_config_rejects_unknown_adapter_option():
    definition = SourceDefinition(
        source_id="source",
        adapter_name="test",
        locator="value",
        adapter_config={"unknown": True},
    )

    with pytest.raises(ConnectorConfigurationError):
        parse_config(definition, BoundedConnectorConfig)


def test_read_bounded_with_missing_path_raises(tmp_path):
    with pytest.raises(SourceContentTooLargeError):
        read_bounded(tmp_path / "missing", 10)


def test_require_media_type_accepts_normalized_wildcard():
    require_media_type(" Audio/MPEG; charset=binary ", ("audio/*",))


def test_guessed_media_type_uses_fallback_for_unknown_suffix():
    assert guessed_media_type(Path("asset.unknown-extension"), "application/fallback") == "application/fallback"


def test_evidence_repository_deduplicates_content_and_retains_each_acquisition(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    repository = EvidenceRepository(database)
    first_item = _source_item()
    second_item = _source_item(
        source_item_id="another-source:item",
        source_id="another-source",
    )
    _persist_source_item(database, first_item)
    _persist_source_item(database, second_item)
    first = _document()
    second = first.model_copy(
        update={
            "asset": first.asset.model_copy(
                update={
                    "source_item_id": "another-source:item",
                    "media_type": "text/html",
                },
            ),
        },
    )

    assert repository.persist(first, source_item=first_item) is True
    assert repository.persist(second, source_item=second_item) is True
    assert repository.persist(second, source_item=second_item) is False
    with database.transaction() as connection:
        asset_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_assets",
        ).fetchone()[0]
        acquisition_rows = connection.execute(
            """
            SELECT source_item_id, media_type
            FROM evidence_asset_acquisitions
            ORDER BY source_item_id
            """,
        ).fetchall()
        fragment_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_fragments",
        ).fetchone()[0]

    assert asset_count == 1
    assert [tuple(row) for row in acquisition_rows] == [
        ("another-source:item", "text/html"),
        ("source:item", "text/plain"),
    ]
    assert fragment_count == 1


def test_persist_ingestion_batch_commits_item_evidence_and_cursor_together(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    item = _source_item()
    _persist_source_item_definition = SourceRepository(database)
    _persist_source_item_definition.register_definition(
        SourceDefinition(
            source_id=item.source_id,
            adapter_name="test",
            locator=item.canonical_uri,
        ),
        registry_version=1,
        registered_at=NOW,
    )
    repository = EvidenceRepository(database)

    result = repository.persist_ingestion_batch(
        item.source_id,
        (SourceIngestionRecord(source_item=item, evidence_document=_document()),),
        next_cursor=SourceCursor(value="next"),
        updated_at=NOW,
    )

    with database.transaction() as connection:
        durable_counts = (
            connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM evidence_assets").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM evidence_asset_acquisitions").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM evidence_fragments").fetchone()[0],
        )
    assert (
        result.persisted_item_count,
        result.changed_evidence_document_count,
        durable_counts,
        SourceRepository(database).get_cursor(item.source_id),
    ) == (1, 1, (1, 1, 1, 1), SourceCursor(value="next"))


def test_persist_ingestion_batch_rolls_back_records_and_cursor_on_late_failure(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    item = _source_item()
    source_repository = SourceRepository(database)
    source_repository.register_definition(
        SourceDefinition(
            source_id=item.source_id,
            adapter_name="test",
            locator=item.canonical_uri,
        ),
        registry_version=1,
        registered_at=NOW,
    )
    wrong_source_item = _source_item(
        source_item_id="other:item",
        source_id="other",
    )
    repository = EvidenceRepository(database)

    with pytest.raises(SourceDiscoveryError):
        repository.persist_ingestion_batch(
            item.source_id,
            (
                SourceIngestionRecord(source_item=item, evidence_document=_document()),
                SourceIngestionRecord(
                    source_item=wrong_source_item,
                    evidence_document=_document(source_item_id=wrong_source_item.source_item_id),
                ),
            ),
            next_cursor=SourceCursor(value="must-not-commit"),
            updated_at=NOW,
        )

    with database.transaction() as connection:
        durable_count = sum(
            (
                connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM evidence_assets").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM evidence_asset_acquisitions").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM evidence_fragments").fetchone()[0],
            )
        )
    assert (durable_count, source_repository.get_cursor(item.source_id)) == (0, None)
