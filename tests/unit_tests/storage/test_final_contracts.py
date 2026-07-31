from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.errors import AssetWriteError
from money_pit.storage.errors import StorageError
from money_pit.storage.sources import SourceRepository
from money_pit.storage.sources import _definition_from_row
from money_pit.storage.sources import _optional_database_text


if TYPE_CHECKING:
    import sqlite3


NOW = datetime(2026, 7, 29, tzinfo=UTC)


@pytest.fixture
def source_repository(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    return SourceRepository(database), database


def _definition(source_id: str = "source") -> SourceDefinition:
    return SourceDefinition(
        source_id=source_id,
        adapter_name="rss",
        locator="https://example.com/feed",
    )


def test_root_returns_configured_asset_root(tmp_path):
    root = tmp_path / "assets"

    assert AssetStore(root).root == root


def test_put_bytes_cleans_temporary_asset_after_atomic_replace_failure(
    tmp_path,
    monkeypatch,
):
    store = AssetStore(tmp_path / "assets")

    def fail_replace(_self: Path, _target: Path) -> Path:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(AssetWriteError):
        store.put_bytes(b"evidence")

    assert not tuple(store.root.rglob("*.tmp"))


def test_register_definition_rejects_nonpositive_registry_version(source_repository):
    repository, _ = source_repository

    with pytest.raises(ValueError, match="registry_version must be positive"):
        repository.register_definition(
            _definition(),
            registry_version=0,
            registered_at=NOW,
        )


def test_list_definitions_returns_stable_order(source_repository):
    repository, _ = source_repository
    repository.register_definition(_definition("z"), registry_version=1, registered_at=NOW)
    repository.register_definition(_definition("a"), registry_version=1, registered_at=NOW)

    assert tuple(item.source_id for item in repository.list_definitions()) == ("a", "z")


def test_get_definition_returns_none_when_unknown(source_repository):
    repository, _ = source_repository

    assert repository.get_definition("missing") is None


def test_persist_discovery_rejects_item_from_another_source(source_repository):
    repository, _ = source_repository
    repository.register_definition(_definition(), registry_version=1, registered_at=NOW)
    wrong_source = SourceItem(
        source_item_id="other:item",
        source_id="other",
        canonical_uri="https://example.com/item",
        discovered_at=NOW,
        content_version="v1",
    )

    with pytest.raises(ValueError, match="belongs to"):
        repository.persist_discovery(
            "source",
            (wrong_source,),
            next_cursor=None,
            updated_at=NOW,
        )


def test_get_definition_wraps_malformed_durable_row(source_repository):
    row = cast(
        "sqlite3.Row",
        cast(
            "object",
            {
                "source_id": "source",
                "adapter_name": "rss",
                "enabled": 1,
                "locator": "https://example.com/feed",
                "cadence": None,
                "tags_json": "not-json",
                "trust_profile": None,
                "allowed_uses_json": "[]",
                "adapter_config_json": "{}",
            },
        ),
    )

    with pytest.raises(StorageError, match="source definition"):
        _definition_from_row(row)


def test__optional_database_text_rejects_non_text():
    with pytest.raises(TypeError, match="timestamp must be text"):
        _optional_database_text(1)
