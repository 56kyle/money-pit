from pathlib import Path

import pytest

from money_pit.storage.assets import AssetStore
from money_pit.storage.errors import AssetWriteError
from money_pit.storage.sources import _optional_database_text


def test_put_bytes_wraps_existing_asset_read_failure(
    tmp_path,
    monkeypatch,
):
    store = AssetStore(tmp_path / "assets")
    stored = store.put_bytes(b"evidence")
    original_read_bytes = Path.read_bytes

    def fail_selected_path(path: Path) -> bytes:
        if path == stored.path:
            raise OSError("unreadable")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_selected_path)

    with pytest.raises(AssetWriteError):
        store.put_bytes(b"evidence")


def test__optional_database_text_returns_text():
    assert _optional_database_text("2026-07-29T00:00:00+00:00") == "2026-07-29T00:00:00+00:00"
