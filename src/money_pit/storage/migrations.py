"""Module containing the single release schema baseline and identity checks."""

# pyright: reportAny=false

import hashlib
import importlib.resources
import json
import sqlite3
from datetime import datetime
from datetime import timezone
from typing import Final

from money_pit.constants import APP_NAME
from money_pit.constants import APP_VERSION
from money_pit.storage.errors import BaselineApplyError
from money_pit.storage.errors import BaselineDiscoveryError
from money_pit.storage.errors import UnknownDatabaseSchemaError


_SCHEMA_PACKAGE: Final[str] = "money_pit.storage.sql"
_BASELINE_FILENAME: Final[str] = "schema_0_0_2.sql"


def load_baseline_sql() -> str:
    """Return the packaged 0.0.2 schema baseline."""
    try:
        return importlib.resources.files(_SCHEMA_PACKAGE).joinpath(_BASELINE_FILENAME).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as error:
        raise BaselineDiscoveryError("The packaged 0.0.2 schema baseline is unavailable.") from error


def _sql_statements(script: str) -> tuple[str, ...]:
    """Split a baseline using SQLite's statement completeness parser."""
    statements: list[str] = []
    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        candidate: str = "".join(pending)
        if sqlite3.complete_statement(candidate):
            if candidate.strip():
                statements.append(candidate)
            pending.clear()
    if "".join(pending).strip():
        raise BaselineDiscoveryError("The packaged baseline ends with an incomplete SQL statement.")
    return tuple(statements)


def _catalog_rows(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    rows: list[sqlite3.Row] = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
        ORDER BY type, name, tbl_name, sql
        """
    ).fetchall()
    return tuple((str(row["type"]), str(row["name"]), str(row["tbl_name"]), str(row["sql"])) for row in rows)


def catalog_fingerprint(connection: sqlite3.Connection) -> str:
    """Return a deterministic digest of the complete application schema catalog."""
    encoded: bytes = json.dumps(_catalog_rows(connection), separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def expected_schema_fingerprint() -> str:
    """Build the packaged baseline in memory and return its catalog digest."""
    connection: sqlite3.Connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        for statement in _sql_statements(load_baseline_sql()):
            _ = connection.execute(statement)
        return catalog_fingerprint(connection)
    except sqlite3.Error as error:
        raise BaselineDiscoveryError("The packaged 0.0.2 baseline is not valid SQLite.") from error
    finally:
        connection.close()


def is_logically_empty(connection: sqlite3.Connection) -> bool:
    """Return whether the database has no application schema objects."""
    return not _catalog_rows(connection)


def apply_baseline(connection: sqlite3.Connection) -> None:
    """Apply the baseline only to a logically empty database."""
    if not is_logically_empty(connection):
        raise UnknownDatabaseSchemaError("A schema baseline can be applied only to an empty database.")
    expected_fingerprint: str = expected_schema_fingerprint()
    try:
        for statement in _sql_statements(load_baseline_sql()):
            _ = connection.execute(statement)
        initialized_at: str = datetime.now(tz=timezone.utc).isoformat()
        _ = connection.execute(
            """
            INSERT INTO schema_metadata (
                singleton, application_id, release, schema_fingerprint, initialized_at
            ) VALUES (1, ?, ?, ?, ?)
            """,
            (APP_NAME, APP_VERSION, expected_fingerprint, initialized_at),
        )
        _ = connection.execute(
            """
            INSERT INTO execution_control (
                control_id, execution_disabled, changed_at, changed_by, reason, policy_version
            ) VALUES (1, 1, ?, 'system', 'Execution is disabled until explicitly enabled.', 'unconfigured')
            """,
            (initialized_at,),
        )
    except sqlite3.Error as error:
        raise BaselineApplyError("Could not apply the 0.0.2 schema baseline.") from error


def verify_schema_identity(connection: sqlite3.Connection) -> None:
    """Fail closed unless metadata and the live schema exactly match this build."""
    expected_fingerprint: str = expected_schema_fingerprint()
    try:
        row: sqlite3.Row | None = connection.execute(
            """
            SELECT application_id, release, schema_fingerprint
            FROM schema_metadata
            WHERE singleton = 1
            """
        ).fetchone()
    except sqlite3.Error as error:
        raise UnknownDatabaseSchemaError("The nonempty database has no recognized schema metadata.") from error
    if row is None:
        raise UnknownDatabaseSchemaError("The nonempty database has no recognized schema metadata.")
    stored_identity: tuple[str, str, str] = (
        str(row["application_id"]),
        str(row["release"]),
        str(row["schema_fingerprint"]),
    )
    expected_identity: tuple[str, str, str] = (APP_NAME, APP_VERSION, expected_fingerprint)
    if stored_identity != expected_identity or catalog_fingerprint(connection) != expected_fingerprint:
        raise UnknownDatabaseSchemaError("The database schema does not exactly match the money-pit 0.0.2 baseline.")
