import json
from datetime import UTC
from datetime import datetime

import pytest

from money_pit.legacy.daily_show import LegacyDailyShowImporter
from money_pit.legacy.daily_show import LegacyImportStatus
from money_pit.storage.database import Database


def test_index_is_read_only_and_idempotent(tmp_path):
    legacy_root = tmp_path / "daily_show"
    run_dir = legacy_root / "2026-07-29_12-30-00"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "signals.json"
    artifact.write_text('{"signal": "hold"}', encoding="utf-8")
    initial_bytes = artifact.read_bytes()
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    importer = LegacyDailyShowImporter(database, legacy_root)
    indexed_at = datetime(2026, 7, 29, 13, tzinfo=UTC)

    first = importer.index(indexed_at=indexed_at)
    second = importer.index(indexed_at=indexed_at)

    assert (first.indexed_count, second.already_indexed_count) == (1, 1)
    assert artifact.read_bytes() == initial_bytes
    assert tuple(path.name for path in run_dir.iterdir()) == ("signals.json",)


def test_index_records_sorted_artifact_names(tmp_path):
    legacy_root = tmp_path / "daily_show"
    run_dir = legacy_root / "not-a-time"
    run_dir.mkdir(parents=True)
    (run_dir / "z.json").write_text("{}", encoding="utf-8")
    (run_dir / "a.txt").write_text("a", encoding="utf-8")
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()

    report = LegacyDailyShowImporter(database, legacy_root).index()

    with database.transaction() as connection:
        row = connection.execute("SELECT inferred_created_at, artifact_names_json FROM legacy_runs").fetchone()
    assert report.diagnostics[0].status is LegacyImportStatus.INDEXED
    assert row["inferred_created_at"] is None
    assert json.loads(row["artifact_names_json"]) == ["a.txt", "z.json"]


def test_index_with_missing_root_returns_skipped_report(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()

    report = LegacyDailyShowImporter(database, tmp_path / "missing").index()

    assert report.scanned_count == 0
    assert report.diagnostics[0].status is LegacyImportStatus.SKIPPED


def test_index_with_file_root_returns_skipped_report(tmp_path):
    legacy_root = tmp_path / "daily_show"
    legacy_root.write_text("not a directory", encoding="utf-8")
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()

    report = LegacyDailyShowImporter(database, legacy_root).index()

    assert report.diagnostics[0].detail == "Legacy root is not a directory."


def test_index_rejects_naive_indexed_at(tmp_path):
    legacy_root = tmp_path / "daily_show"
    legacy_root.mkdir()
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()

    with pytest.raises(ValueError, match="indexed_at must be timezone-aware"):
        LegacyDailyShowImporter(database, legacy_root).index(
            indexed_at=datetime(2026, 7, 29),  # noqa: DTZ001 - intentionally naive boundary input
        )
