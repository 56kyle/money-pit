import importlib.resources
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from money_pit.constants import APP_NAME
from money_pit.constants import APP_VERSION
from money_pit.storage import migrations as migrations_module
from money_pit.storage.database import Database
from money_pit.storage.errors import BaselineApplyError
from money_pit.storage.errors import BaselineDiscoveryError
from money_pit.storage.errors import UnknownDatabaseSchemaError
from money_pit.storage.migrations import catalog_fingerprint
from money_pit.storage.migrations import expected_schema_fingerprint


_PREDECESSOR_RELEASE = "0.0.2"
_INITIALIZED_AT = "2026-08-01T12:00:00+00:00"


def _create_exact_predecessor(path: Path) -> str:
    baseline = (
        importlib.resources.files("money_pit.storage.sql").joinpath("schema_0_0_2.sql").read_text(encoding="utf-8")
    )
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        _ = connection.executescript(baseline)
        fingerprint = catalog_fingerprint(connection)
        _ = connection.execute(
            "INSERT INTO schema_metadata VALUES (1, ?, ?, ?, ?)",
            (APP_NAME, _PREDECESSOR_RELEASE, fingerprint, _INITIALIZED_AT),
        )
        _ = connection.execute(
            "INSERT INTO source_cursors VALUES ('source', 'sync', '{\"page\":2}', ?)",
            (_INITIALIZED_AT,),
        )
        _ = connection.execute(
            "INSERT INTO execution_control VALUES (1, 1, ?, 'tester', 'preserve', 'policy-1')",
            (_INITIALIZED_AT,),
        )
        connection.commit()
    finally:
        connection.close()
    return fingerprint


def _logical_snapshot(path: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(path)
    try:
        return (
            connection.execute(
                "SELECT application_id, release, schema_fingerprint, initialized_at FROM schema_metadata"
            ).fetchall(),
            connection.execute("SELECT * FROM source_cursors").fetchall(),
            connection.execute("SELECT * FROM execution_control").fetchall(),
            connection.execute(
                """SELECT type, name, tbl_name, sql FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
                ORDER BY type, name, tbl_name, sql"""
            ).fetchall(),
        )
    finally:
        connection.close()


def _index_columns(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = cast(
        "list[sqlite3.Row]",
        connection.execute("PRAGMA index_info(claim_interpretation_success_idx)").fetchall(),
    )
    return tuple(str(cast("object", row[2])) for row in rows)


def _row_values(row: sqlite3.Row | None) -> tuple[object, ...] | None:
    return None if row is None else tuple(row)


def test_load_baseline_sql_rejects_application_schema_release_mismatch_before_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor_loaded = False

    def observe_predecessor_load() -> str:
        nonlocal predecessor_loaded
        predecessor_loaded = True
        return ""

    monkeypatch.setattr(migrations_module, "APP_VERSION", "0.0.4")
    monkeypatch.setattr(migrations_module, "_load_predecessor_baseline_sql", observe_predecessor_load)

    with pytest.raises(BaselineDiscoveryError):
        _ = migrations_module.load_baseline_sql()

    assert predecessor_loaded is False


def test_initialize_installs_the_exact_release_baseline(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")

    database.initialize()

    with database.transaction() as connection:
        row = cast(
            "sqlite3.Row | None",
            connection.execute(
                "SELECT application_id, release, schema_fingerprint FROM schema_metadata",
            ).fetchone(),
        )
        assert row is not None
        assert tuple(row) == (APP_NAME, APP_VERSION, expected_schema_fingerprint())
        assert catalog_fingerprint(connection) == expected_schema_fingerprint()


def test_initialize_indexes_successful_interpretations_by_exact_asset(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()

    with database.transaction() as connection:
        index_columns = _index_columns(connection)

    assert index_columns == (
        "source_item_id",
        "content_version",
        "asset_id",
        "interpreter_version",
    )


def test_initialize_atomically_migrates_exact_0_0_2_and_preserves_rows(tmp_path: Path) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)

    Database(path).initialize()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        metadata = _row_values(
            cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT application_id, release, schema_fingerprint, initialized_at FROM schema_metadata"
                ).fetchone(),
            )
        )
        source_cursor = _row_values(
            cast("sqlite3.Row | None", connection.execute("SELECT * FROM source_cursors").fetchone())
        )
        execution_control = _row_values(
            cast("sqlite3.Row | None", connection.execute("SELECT * FROM execution_control").fetchone())
        )
        index_columns = _index_columns(connection)
        migrated_fingerprint = catalog_fingerprint(connection)
    finally:
        connection.close()
    assert (
        metadata,
        source_cursor,
        execution_control,
        index_columns,
        migrated_fingerprint,
    ) == (
        (APP_NAME, APP_VERSION, expected_schema_fingerprint(), _INITIALIZED_AT),
        ("source", "sync", '{"page":2}', _INITIALIZED_AT),
        (1, 1, _INITIALIZED_AT, "tester", "preserve", "policy-1"),
        ("source_item_id", "content_version", "asset_id", "interpreter_version"),
        expected_schema_fingerprint(),
    )


def test_initialize_is_idempotent_after_0_0_2_migration(tmp_path: Path) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    database = Database(path)
    database.initialize()
    migrated = _logical_snapshot(path)

    database.initialize()

    assert _logical_snapshot(path) == migrated


def test_initialize_rolls_back_predecessor_when_post_index_migration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    predecessor = _logical_snapshot(path)
    catalog_fingerprint = migrations_module.catalog_fingerprint

    def fail_after_target_index_creation(connection: sqlite3.Connection) -> str:
        fingerprint = catalog_fingerprint(connection)
        database_row = cast(
            "sqlite3.Row | None",
            connection.execute("PRAGMA database_list").fetchone(),
        )
        database_path = None if database_row is None else Path(str(cast("object", database_row[2])))
        if database_path == path and _index_columns(connection) == (
            "source_item_id",
            "content_version",
            "asset_id",
            "interpreter_version",
        ):
            raise BaselineApplyError("Injected post-index migration failure.")
        return fingerprint

    monkeypatch.setattr(migrations_module, "catalog_fingerprint", fail_after_target_index_creation)

    with pytest.raises(BaselineApplyError):
        Database(path).initialize()

    assert _logical_snapshot(path) == predecessor


def test_initialize_refuses_tampered_0_0_2_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "tampered-predecessor.sqlite3"
    _ = _create_exact_predecessor(path)
    connection = sqlite3.connect(path)
    _ = connection.execute("CREATE TABLE tampered_history (value TEXT NOT NULL)")
    _ = connection.execute("INSERT INTO tampered_history VALUES ('preserve-me')")
    connection.commit()
    connection.close()
    before = _logical_snapshot(path)

    with pytest.raises(UnknownDatabaseSchemaError):
        Database(path).initialize()

    assert _logical_snapshot(path) == before


def test_initialize_refuses_an_unknown_nonempty_database_without_rewriting_it(tmp_path: Path) -> None:
    path = tmp_path / "unknown.sqlite3"
    connection = sqlite3.connect(path)
    _ = connection.execute("CREATE TABLE user_history (value TEXT NOT NULL)")
    _ = connection.execute("INSERT INTO user_history VALUES ('preserve-me')")
    connection.commit()
    connection.close()

    with pytest.raises(UnknownDatabaseSchemaError):
        Database(path).initialize()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT value FROM user_history").fetchone() == ("preserve-me",)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name = 'schema_metadata'",
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_initialize_refuses_catalog_drift_from_the_recorded_baseline(tmp_path: Path) -> None:
    path = tmp_path / "drifted.sqlite3"
    database = Database(path)
    database.initialize()
    connection = sqlite3.connect(path)
    _ = connection.execute("CREATE TABLE unexpected_runtime_table (id INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(UnknownDatabaseSchemaError):
        database.initialize()
