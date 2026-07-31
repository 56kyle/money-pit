"""Module containing ordered SQLite migrations for the money_pit package."""
# pyright: reportAny=false

import hashlib
import importlib.resources
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Final

from money_pit.storage.errors import MigrationApplyError
from money_pit.storage.errors import MigrationChecksumError
from money_pit.storage.errors import MigrationDiscoveryError
from money_pit.storage.errors import MigrationHistoryError


_MIGRATION_PACKAGE: Final[str] = "money_pit.storage.sql"
_MIGRATION_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql")


@dataclass(frozen=True)
class Migration:
    """One immutable, checksummed schema migration."""

    version: int
    name: str
    sql: str
    checksum: str


def discover_migrations() -> tuple[Migration, ...]:
    """Return packaged migrations in validated version order."""
    resources = importlib.resources.files(_MIGRATION_PACKAGE)
    migrations: list[Migration] = []
    for resource in resources.iterdir():
        match: re.Match[str] | None = _MIGRATION_NAME_PATTERN.fullmatch(resource.name)
        if match is None:
            continue
        sql: str = resource.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    migrations.sort(key=lambda migration: migration.version)
    versions: tuple[int, ...] = tuple(migration.version for migration in migrations)
    expected_versions: tuple[int, ...] = tuple(range(1, len(migrations) + 1))
    if not migrations or versions != expected_versions:
        raise MigrationDiscoveryError(
            f"Packaged migrations must be a non-empty contiguous sequence starting at 0001; found {versions}."
        )
    return tuple(migrations)


def _sql_statements(script: str) -> tuple[str, ...]:
    """Split a migration script using SQLite's own completeness parser."""
    statements: list[str] = []
    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        candidate: str = "".join(pending)
        if sqlite3.complete_statement(candidate):
            if candidate.strip():  # pragma: no branch - complete SQLite statements are non-empty here.
                statements.append(candidate)
            pending.clear()
    if "".join(pending).strip():
        raise MigrationDiscoveryError("Packaged migration ends with an incomplete SQL statement.")
    return tuple(statements)


def _applied_migrations(connection: sqlite3.Connection) -> tuple[tuple[int, str, str], ...]:
    rows: list[sqlite3.Row] = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return tuple((int(row["version"]), str(row["name"]), str(row["checksum"])) for row in rows)


def apply_pending_migrations(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...] | None = None,
) -> None:
    """Apply the unapplied suffix after verifying immutable migration history."""
    selected: tuple[Migration, ...] = migrations if migrations is not None else discover_migrations()
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied: tuple[tuple[int, str, str], ...] = _applied_migrations(connection)
    if len(applied) > len(selected):
        raise MigrationHistoryError("Database contains migrations not present in this application build.")
    for index, (version, name, checksum) in enumerate(applied):
        packaged: Migration = selected[index]
        if version != packaged.version or name != packaged.name:
            raise MigrationHistoryError(
                f"Database migration {version}:{name} is not the expected {packaged.version}:{packaged.name}."
            )
        if checksum != packaged.checksum:
            raise MigrationChecksumError(f"Database migration {version}:{name} does not match its packaged checksum.")

    for migration in selected[len(applied) :]:
        try:
            for statement in _sql_statements(migration.sql):
                connection.execute(statement)
            applied_at: str = datetime.now(tz=timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (migration.version, migration.name, migration.checksum, applied_at),
            )
        except sqlite3.Error as error:
            raise MigrationApplyError(f"Could not apply migration {migration.version}:{migration.name}.") from error
