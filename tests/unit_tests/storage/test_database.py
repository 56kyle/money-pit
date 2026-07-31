import sqlite3

import pytest

from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.migrations import discover_migrations


def test_initialize_applies_each_migration_once(tmp_path):
    database = Database(tmp_path / "state.sqlite3")

    database.initialize()
    database.initialize()

    with database.transaction() as connection:
        rows = connection.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version").fetchall()
    migrations = discover_migrations()
    assert tuple((row["version"], row["name"], row["checksum"]) for row in rows) == tuple(
        (migration.version, migration.name, migration.checksum) for migration in migrations
    )


def test_initialize_defaults_execution_to_disabled(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()

    with database.transaction() as connection:
        row = connection.execute(
            "SELECT execution_disabled, changed_by FROM execution_control WHERE control_id = 1"
        ).fetchone()

    assert row is not None
    assert (row["execution_disabled"], row["changed_by"]) == (1, "migration")


def test_transaction_rolls_back_application_failure(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()

    with pytest.raises(RuntimeError):  # noqa: PT012, SIM117
        with database.transaction(TransactionMode.WRITE) as connection:
            connection.execute(
                """
                INSERT INTO source_cursors (source_id, cursor_json, updated_at)
                VALUES ('source', '{}', '2026-01-01T00:00:00+00:00')
                """
            )
            raise RuntimeError("application failure")

    with database.transaction() as connection:
        count = connection.execute("SELECT COUNT(*) FROM source_cursors").fetchone()[0]
    assert count == 0


def test_transaction_wraps_sqlite_failure(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()

    from money_pit.storage.errors import StorageTransactionError

    with pytest.raises(StorageTransactionError):  # noqa: SIM117
        with database.transaction(TransactionMode.WRITE) as connection:
            connection.execute("INSERT INTO table_that_does_not_exist VALUES (1)")


def test_database_enforces_foreign_keys(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()

    with pytest.raises(sqlite3.IntegrityError):  # noqa: PT012
        connection = sqlite3.connect(database.path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO evidence_fragments (
                    fragment_id, asset_id, fragment_kind, locator_json, extraction_method
                ) VALUES ('fragment', 'missing', 'page', '{}', 'test')
                """
            )
        finally:
            connection.close()
