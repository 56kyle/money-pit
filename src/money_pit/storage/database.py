"""Module containing the SQLite transaction boundary for the money_pit package."""
# pyright: reportAny=false

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Final

from money_pit.storage.errors import SchemaIdentityError
from money_pit.storage.errors import StorageCapabilityError
from money_pit.storage.errors import StorageConnectionError
from money_pit.storage.errors import StorageTransactionError
from money_pit.storage.migrations import apply_baseline
from money_pit.storage.migrations import is_logically_empty
from money_pit.storage.migrations import verify_schema_identity


DEFAULT_BUSY_TIMEOUT_MS: Final[int] = 5_000


class TransactionMode(StrEnum):
    """SQLite transaction modes supported by the storage boundary."""

    READ = "DEFERRED"
    WRITE = "IMMEDIATE"


class Database:
    """Own configured SQLite connections and the transaction choke point."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        """Bind the database boundary to a path and positive busy timeout."""
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive.")
        self._path: Path = path
        self._busy_timeout_ms: int = busy_timeout_ms

    @property
    def path(self) -> Path:
        """Return the database path."""
        return self._path

    def initialize(self) -> None:
        """Create 0.0.4, verify it, or migrate an exact 0.0.3 predecessor."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction(TransactionMode.WRITE) as connection:
            if is_logically_empty(connection):
                apply_baseline(connection)
            else:
                verify_schema_identity(connection)

    @contextmanager
    def transaction(
        self,
        mode: TransactionMode = TransactionMode.READ,
    ) -> Generator[sqlite3.Connection]:
        """Yield one configured transaction and close it after commit or rollback."""
        connection: sqlite3.Connection = self._connect()
        began: bool = False
        try:
            _ = connection.execute(f"BEGIN {mode.value}")
            began = True
            yield connection
            connection.commit()
        except (SchemaIdentityError, StorageCapabilityError):
            if began:  # pragma: no branch - failures here occur only after BEGIN succeeds.
                connection.rollback()
            raise
        except sqlite3.Error as error:
            if began:
                try:
                    connection.rollback()
                except sqlite3.Error as rollback_error:
                    raise StorageTransactionError(
                        "SQLite transaction failed and could not be rolled back."
                    ) from rollback_error
            raise StorageTransactionError("SQLite transaction failed.") from error
        except BaseException:
            if began:  # pragma: no branch - failures here occur only after BEGIN succeeds.
                try:
                    connection.rollback()
                except sqlite3.Error as rollback_error:
                    raise StorageTransactionError(
                        "Application operation failed and its transaction could not be rolled back."
                    ) from rollback_error
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        """Open and validate one SQLite connection."""
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self._path,
                timeout=self._busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            _ = connection.execute("PRAGMA foreign_keys = ON")
            _ = connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            self._require_capabilities(connection)
        except StorageCapabilityError:
            if connection is not None:  # pragma: no branch - capability checks require a live connection.
                connection.close()
            raise
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise StorageConnectionError(f"Could not open SQLite database at {self._path}.") from error
        return connection

    def _require_capabilities(self, connection: sqlite3.Connection) -> None:
        """Raise StorageCapabilityError unless foreign keys, JSON, and FTS5 work."""
        foreign_keys_row: sqlite3.Row | None = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys_row is None or int(foreign_keys_row[0]) != 1:
            raise StorageCapabilityError("SQLite foreign-key enforcement could not be enabled.")
        try:
            json_row: sqlite3.Row | None = connection.execute("SELECT json_valid('{\"ok\": true}')").fetchone()
            if json_row is None or int(json_row[0]) != 1:
                raise StorageCapabilityError("SQLite JSON functions are unavailable.")
            _ = connection.execute("CREATE VIRTUAL TABLE temp.money_pit_fts5_capability USING fts5(content)")
            _ = connection.execute("DROP TABLE temp.money_pit_fts5_capability")
        except sqlite3.Error as error:
            raise StorageCapabilityError("SQLite FTS5 support is unavailable.") from error
