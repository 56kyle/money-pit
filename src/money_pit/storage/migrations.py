"""Module containing release schema baselines, migration, and identity checks."""

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
_CURRENT_SCHEMA_RELEASE: Final[str] = "0.0.3"
_PREDECESSOR_RELEASE: Final[str] = "0.0.2"
_PREDECESSOR_BASELINE_FILENAME: Final[str] = "schema_0_0_2.sql"
_PREDECESSOR_SUCCESS_INDEX: Final[str] = """CREATE UNIQUE INDEX claim_interpretation_success_idx
ON claim_interpretation_attempts (
    source_item_id, content_version, interpreter_version
)
WHERE outcome = 'succeeded';"""
_CURRENT_SUCCESS_INDEX: Final[str] = """CREATE UNIQUE INDEX claim_interpretation_success_idx
ON claim_interpretation_attempts (
    source_item_id, content_version, asset_id, interpreter_version
)
WHERE outcome = 'succeeded';"""


def _require_current_schema_release() -> None:
    """Fail closed when application and schema releases are not intentionally aligned."""
    if APP_VERSION != _CURRENT_SCHEMA_RELEASE:
        raise BaselineDiscoveryError(
            f"Application release {APP_VERSION} is not bound to schema release {_CURRENT_SCHEMA_RELEASE}."
        )


def _load_predecessor_baseline_sql() -> str:
    """Return the retained, trusted 0.0.2 schema baseline."""
    try:
        return (
            importlib.resources.files(_SCHEMA_PACKAGE)
            .joinpath(_PREDECESSOR_BASELINE_FILENAME)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError) as error:
        raise BaselineDiscoveryError("The packaged 0.0.2 predecessor baseline is unavailable.") from error


def load_baseline_sql() -> str:
    """Return the 0.0.3 baseline derived from the retained predecessor."""
    _require_current_schema_release()
    predecessor_sql: str = _load_predecessor_baseline_sql()
    occurrence_count: int = predecessor_sql.count(_PREDECESSOR_SUCCESS_INDEX)
    if occurrence_count != 1:
        raise BaselineDiscoveryError("The packaged 0.0.2 predecessor index definition is not trusted.")
    return predecessor_sql.replace(_PREDECESSOR_SUCCESS_INDEX, _CURRENT_SUCCESS_INDEX)


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


def _baseline_fingerprint(script: str, *, release: str) -> str:
    """Build one packaged baseline in memory and return its catalog digest."""
    connection: sqlite3.Connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        for statement in _sql_statements(script):
            _ = connection.execute(statement)
        return catalog_fingerprint(connection)
    except sqlite3.Error as error:
        raise BaselineDiscoveryError(f"The packaged {release} baseline is not valid SQLite.") from error
    finally:
        connection.close()


def expected_schema_fingerprint() -> str:
    """Return the catalog digest of the packaged 0.0.3 baseline."""
    _require_current_schema_release()
    return _baseline_fingerprint(load_baseline_sql(), release=_CURRENT_SCHEMA_RELEASE)


def _expected_predecessor_schema_fingerprint() -> str:
    """Return the catalog digest of the retained 0.0.2 baseline."""
    return _baseline_fingerprint(_load_predecessor_baseline_sql(), release=_PREDECESSOR_RELEASE)


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
            (APP_NAME, _CURRENT_SCHEMA_RELEASE, expected_fingerprint, initialized_at),
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
        raise BaselineApplyError(f"Could not apply the {_CURRENT_SCHEMA_RELEASE} schema baseline.") from error


def _schema_identity(connection: sqlite3.Connection) -> tuple[str, str, str]:
    """Read the recognized schema identity from a nonempty database."""
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
    return (str(row["application_id"]), str(row["release"]), str(row["schema_fingerprint"]))


def _migrate_verified_predecessor(connection: sqlite3.Connection, *, target_fingerprint: str) -> None:
    """Migrate an exact 0.0.2 catalog to 0.0.3 without changing stored data."""
    try:
        _ = connection.execute("DROP INDEX claim_interpretation_success_idx")
        _ = connection.execute(_CURRENT_SUCCESS_INDEX)
    except sqlite3.Error as error:
        raise BaselineApplyError("Could not migrate the verified 0.0.2 schema to 0.0.3.") from error
    if catalog_fingerprint(connection) != target_fingerprint:
        raise BaselineApplyError("The migrated 0.0.3 schema does not match the packaged baseline.")
    try:
        cursor: sqlite3.Cursor = connection.execute(
            """
            UPDATE schema_metadata
            SET release = ?, schema_fingerprint = ?
            WHERE singleton = 1
            """,
            (_CURRENT_SCHEMA_RELEASE, target_fingerprint),
        )
    except sqlite3.Error as error:
        raise BaselineApplyError("Could not record the verified 0.0.3 schema identity.") from error
    if cursor.rowcount != 1:
        raise BaselineApplyError("Could not record the verified 0.0.3 schema identity.")


def verify_schema_identity(connection: sqlite3.Connection) -> None:
    """Verify this build or atomically migrate its exact trusted predecessor."""
    expected_fingerprint: str = expected_schema_fingerprint()
    stored_identity: tuple[str, str, str] = _schema_identity(connection)
    expected_identity: tuple[str, str, str] = (APP_NAME, _CURRENT_SCHEMA_RELEASE, expected_fingerprint)
    live_fingerprint: str = catalog_fingerprint(connection)
    if stored_identity == expected_identity and live_fingerprint == expected_fingerprint:
        return

    predecessor_fingerprint: str = _expected_predecessor_schema_fingerprint()
    predecessor_identity: tuple[str, str, str] = (
        APP_NAME,
        _PREDECESSOR_RELEASE,
        predecessor_fingerprint,
    )
    if stored_identity == predecessor_identity and live_fingerprint == predecessor_fingerprint:
        _migrate_verified_predecessor(connection, target_fingerprint=expected_fingerprint)
        if _schema_identity(connection) == expected_identity:
            return
        raise BaselineApplyError("The migrated database did not retain the verified 0.0.3 identity.")

    detail: str = " ".join(
        (
            f"The database schema does not exactly match money-pit {_CURRENT_SCHEMA_RELEASE}",
            "or its trusted predecessor.",
        )
    )
    raise UnknownDatabaseSchemaError(detail)
