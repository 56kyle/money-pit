# pyright: reportPrivateUsage=false, reportUnusedCallResult=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false

import sqlite3
from pathlib import Path
from typing import cast

import pytest
from pytest import MonkeyPatch

from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import MigrationApplyError
from money_pit.storage.errors import StorageCapabilityError
from money_pit.storage.errors import StorageConnectionError
from money_pit.storage.errors import StorageTransactionError


class _FailingConnection:
    def __init__(
        self,
        *,
        execute_error: sqlite3.Error | None = None,
        commit_error: sqlite3.Error | None = None,
        rollback_error: sqlite3.Error | None = None,
    ) -> None:
        self.execute_error: sqlite3.Error | None = execute_error
        self.commit_error: sqlite3.Error | None = commit_error
        self.rollback_error: sqlite3.Error | None = rollback_error
        self.rollback_count: int = 0
        self.closed: bool = False

    def execute(self, _statement: str) -> None:
        if self.execute_error is not None:
            raise self.execute_error

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollback_count += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.closed = True


def _database_with_connection(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    connection: _FailingConnection,
) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    monkeypatch.setattr(database, "_connect", lambda: connection)
    return database


def test_transaction_wraps_begin_failure_and_closes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    connection = _FailingConnection(execute_error=sqlite3.OperationalError("begin"))
    database = _database_with_connection(tmp_path, monkeypatch, connection)

    with pytest.raises(StorageTransactionError, match="SQLite transaction failed"), database.transaction():
        pass

    assert (connection.rollback_count, connection.closed) == (0, True)


def test_transaction_rolls_back_commit_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    connection = _FailingConnection(commit_error=sqlite3.OperationalError("commit"))
    database = _database_with_connection(tmp_path, monkeypatch, connection)

    with pytest.raises(StorageTransactionError, match="SQLite transaction failed"), database.transaction():
        pass

    assert connection.rollback_count == 1


def test_transaction_reports_failed_sqlite_rollback(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    connection = _FailingConnection(
        commit_error=sqlite3.OperationalError("commit"),
        rollback_error=sqlite3.OperationalError("rollback"),
    )
    database = _database_with_connection(tmp_path, monkeypatch, connection)

    with (
        pytest.raises(
            StorageTransactionError,
            match="could not be rolled back",
        ) as raised,
        database.transaction(),
    ):
        pass

    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)


def test_transaction_reports_failed_application_rollback(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    connection = _FailingConnection(rollback_error=sqlite3.OperationalError("rollback"))
    database = _database_with_connection(tmp_path, monkeypatch, connection)

    with (
        pytest.raises(
            StorageTransactionError,
            match="Application operation failed",
        ) as raised,
        database.transaction(),
    ):
        raise RuntimeError("application")

    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)


def test_transaction_reraises_migration_error_after_rollback(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    connection = _FailingConnection()
    database = _database_with_connection(tmp_path, monkeypatch, connection)

    with pytest.raises(MigrationApplyError, match="migration"), database.transaction(TransactionMode.WRITE):
        raise MigrationApplyError("migration")

    assert connection.rollback_count == 1


def test__connect_wraps_sqlite_open_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.sqlite3")

    def fail(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("offline")

    monkeypatch.setattr(sqlite3, "connect", fail)

    with pytest.raises(StorageConnectionError, match="Could not open SQLite database"):
        _ = database._connect()


def test__connect_closes_connection_after_capability_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    database = Database(tmp_path / "state.sqlite3")

    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)

    def fail(_connection: sqlite3.Connection) -> None:
        raise StorageCapabilityError("unsupported")

    monkeypatch.setattr(database, "_require_capabilities", fail)

    with pytest.raises(StorageCapabilityError, match="unsupported"):
        _ = database._connect()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


class _CapabilityResult:
    def __init__(self, row: tuple[int] | None) -> None:
        self._row: tuple[int] | None = row

    def fetchone(self) -> tuple[int] | None:
        return self._row


class _CapabilityConnection:
    def __init__(
        self,
        *,
        foreign_keys: tuple[int] | None = (1,),
        json_result: tuple[int] | None = (1,),
        fts_error: sqlite3.Error | None = None,
    ) -> None:
        self.foreign_keys: tuple[int] | None = foreign_keys
        self.json_result: tuple[int] | None = json_result
        self.fts_error: sqlite3.Error | None = fts_error

    def execute(self, statement: str) -> _CapabilityResult:
        if statement == "PRAGMA foreign_keys":
            return _CapabilityResult(self.foreign_keys)
        if statement.startswith("SELECT json_valid"):
            return _CapabilityResult(self.json_result)
        if statement.startswith("CREATE VIRTUAL TABLE") and self.fts_error is not None:
            raise self.fts_error
        return _CapabilityResult(None)


def _as_sqlite_connection(connection: _CapabilityConnection) -> sqlite3.Connection:
    return cast("sqlite3.Connection", cast("object", connection))


@pytest.mark.parametrize("foreign_keys", [None, (0,)])
def test__require_capabilities_rejects_missing_foreign_keys(
    tmp_path: Path,
    foreign_keys: tuple[int] | None,
) -> None:
    database = Database(tmp_path / "state.sqlite3")

    with pytest.raises(StorageCapabilityError, match="foreign-key"):
        database._require_capabilities(
            _as_sqlite_connection(_CapabilityConnection(foreign_keys=foreign_keys)),
        )


@pytest.mark.parametrize("json_result", [None, (0,)])
def test__require_capabilities_rejects_missing_json(
    tmp_path: Path,
    json_result: tuple[int] | None,
) -> None:
    database = Database(tmp_path / "state.sqlite3")

    with pytest.raises(StorageCapabilityError, match="JSON"):
        database._require_capabilities(
            _as_sqlite_connection(_CapabilityConnection(json_result=json_result)),
        )


def test__require_capabilities_wraps_missing_fts5(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")

    with pytest.raises(StorageCapabilityError, match="FTS5"):
        database._require_capabilities(
            _as_sqlite_connection(_CapabilityConnection(fts_error=sqlite3.OperationalError("missing"))),
        )
