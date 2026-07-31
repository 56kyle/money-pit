from datetime import UTC
from datetime import datetime

import pytest

from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError
from money_pit.storage.sources import SourceItemCollisionError
from money_pit.storage.sources import SourceRepository


NOW = datetime(2026, 7, 29, tzinfo=UTC)


@pytest.fixture
def source_repository(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    return SourceRepository(database), database


def _definition(locator: str = "https://example.com/feed") -> SourceDefinition:
    return SourceDefinition(source_id="source", adapter_name="rss", locator=locator, tags=("macro",))


def _item(uri: str = "https://example.com/item") -> SourceItem:
    return SourceItem(
        source_item_id="source:item",
        source_id="source",
        canonical_uri=uri,
        discovered_at=NOW,
        content_version="v1",
    )


def test_register_definition_is_idempotent_and_updates(source_repository):
    repository, _ = source_repository

    first = repository.register_definition(_definition(), registry_version=1, registered_at=NOW)
    duplicate = repository.register_definition(_definition(), registry_version=1, registered_at=NOW)
    changed = repository.register_definition(
        _definition("https://example.com/new"), registry_version=1, registered_at=NOW
    )

    assert (first, duplicate, changed) == (True, False, True)
    assert repository.get_definition("source") == _definition("https://example.com/new")


def test_persist_discovery_is_atomic_and_idempotent(source_repository):
    repository, _ = source_repository
    repository.register_definition(_definition(), registry_version=1, registered_at=NOW)
    cursor = SourceCursor(value="next")

    first = repository.persist_discovery("source", (_item(),), next_cursor=cursor, updated_at=NOW)
    duplicate = repository.persist_discovery("source", (_item(),), next_cursor=cursor, updated_at=NOW)

    assert (first, duplicate) == (1, 0)
    assert repository.get_cursor("source") == cursor


def test_persist_discovery_rejects_identity_collision(source_repository):
    repository, _ = source_repository
    repository.register_definition(_definition(), registry_version=1, registered_at=NOW)
    repository.persist_discovery("source", (_item(),), next_cursor=None, updated_at=NOW)

    with pytest.raises(SourceItemCollisionError):
        repository.persist_discovery(
            "source",
            (_item("https://example.com/changed"),),
            next_cursor=None,
            updated_at=NOW,
        )


def test_get_cursor_rejects_malformed_durable_json(source_repository):
    repository, database = source_repository
    repository.register_definition(_definition(), registry_version=1, registered_at=NOW)
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute(
            "INSERT INTO source_cursors (source_id, cursor_purpose, cursor_json, updated_at) VALUES (?, ?, ?, ?)",
            ("source", SourceCursorPurpose.SYNC, "{}", NOW.isoformat()),
        )

    with pytest.raises(StorageError):
        repository.get_cursor("source")


def test_register_definition_rejects_naive_timestamp(source_repository):
    repository, _ = source_repository

    with pytest.raises(ValueError, match="registered_at must be timezone-aware"):
        repository.register_definition(
            _definition(),
            registry_version=1,
            registered_at=NOW.replace(tzinfo=None),
        )


def test_cursor_purposes_are_independent_and_clearable(source_repository):
    repository, _ = source_repository
    repository.register_definition(_definition(), registry_version=1, registered_at=NOW)
    sync_cursor = SourceCursor(value="live")
    backfill_cursor = SourceCursor(value="historical-page-2")

    repository.persist_discovery(
        "source",
        (),
        next_cursor=sync_cursor,
        updated_at=NOW,
    )
    repository.persist_discovery(
        "source",
        (),
        next_cursor=backfill_cursor,
        updated_at=NOW,
        cursor_purpose=SourceCursorPurpose.BACKFILL,
    )

    assert repository.get_cursor("source") == sync_cursor
    assert (
        repository.get_cursor(
            "source",
            purpose=SourceCursorPurpose.BACKFILL,
        )
        == backfill_cursor
    )
    assert (
        repository.clear_cursor(
            "source",
            purpose=SourceCursorPurpose.BACKFILL,
        )
        is True
    )
    assert repository.get_cursor("source") == sync_cursor
