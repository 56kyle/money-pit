import sqlite3
from pathlib import Path
from typing import cast

import pytest

from money_pit.constants import APP_NAME
from money_pit.constants import APP_VERSION
from money_pit.storage.database import Database
from money_pit.storage.errors import UnknownDatabaseSchemaError
from money_pit.storage.migrations import catalog_fingerprint
from money_pit.storage.migrations import expected_schema_fingerprint


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
