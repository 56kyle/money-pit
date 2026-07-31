"""Module containing durable point-in-time portfolio snapshot storage."""

import sqlite3
from enum import StrEnum
from typing import cast

from pydantic import ValidationError

from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


class SnapshotRepositoryError(StorageError):
    """Base class for invalid durable snapshot state."""


class SnapshotNotFoundError(SnapshotRepositoryError):
    """Raised when a requested snapshot is absent."""


class MalformedSnapshotRecordError(SnapshotRepositoryError):
    """Raised when persisted snapshot JSON violates its content fingerprint."""


class _SnapshotKind(StrEnum):
    PORTFOLIO = "portfolio"
    MARKET = "market"


_INSERT_SQL: dict[_SnapshotKind, str] = {
    _SnapshotKind.PORTFOLIO: """
        INSERT INTO portfolio_state_snapshots (snapshot_id, captured_at, payload_json)
        VALUES (?, ?, ?)
        ON CONFLICT(snapshot_id) DO NOTHING
    """,
    _SnapshotKind.MARKET: """
        INSERT INTO market_state_snapshots (snapshot_id, captured_at, payload_json)
        VALUES (?, ?, ?)
        ON CONFLICT(snapshot_id) DO NOTHING
    """,
}
_SELECT_SQL: dict[_SnapshotKind, str] = {
    _SnapshotKind.PORTFOLIO: "SELECT payload_json FROM portfolio_state_snapshots WHERE snapshot_id = ?",
    _SnapshotKind.MARKET: "SELECT payload_json FROM market_state_snapshots WHERE snapshot_id = ?",
}


class SnapshotRepository:
    """Append and retrieve immutable content-addressed state snapshots."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to an initialized database."""
        self._database: Database = database

    def append_portfolio(self, snapshot: PortfolioStateSnapshot) -> None:
        """Persist one immutable portfolio snapshot idempotently."""
        self._append(
            kind=_SnapshotKind.PORTFOLIO,
            snapshot_id=snapshot.snapshot_id,
            captured_at=snapshot.payload.captured_at.isoformat(),
            payload_json=snapshot.model_dump_json(),
        )

    def append_market(self, snapshot: MarketStateSnapshot) -> None:
        """Persist one immutable market snapshot idempotently."""
        self._append(
            kind=_SnapshotKind.MARKET,
            snapshot_id=snapshot.snapshot_id,
            captured_at=snapshot.payload.captured_at.isoformat(),
            payload_json=snapshot.model_dump_json(),
        )

    def get_portfolio(self, snapshot_id: str) -> PortfolioStateSnapshot:
        """Return one fingerprint-validated portfolio snapshot."""
        serialized: str = self._get(_SnapshotKind.PORTFOLIO, snapshot_id)
        try:
            return PortfolioStateSnapshot.model_validate_json(serialized)
        except (ValueError, ValidationError) as error:
            raise MalformedSnapshotRecordError("stored portfolio snapshot is malformed") from error

    def get_market(self, snapshot_id: str) -> MarketStateSnapshot:
        """Return one fingerprint-validated market snapshot."""
        serialized: str = self._get(_SnapshotKind.MARKET, snapshot_id)
        try:
            return MarketStateSnapshot.model_validate_json(serialized)
        except (ValueError, ValidationError) as error:
            raise MalformedSnapshotRecordError("stored market snapshot is malformed") from error

    def _append(
        self,
        *,
        kind: _SnapshotKind,
        snapshot_id: str,
        captured_at: str,
        payload_json: str,
    ) -> None:
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                _INSERT_SQL[kind],
                (snapshot_id, captured_at, payload_json),
            )
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    _SELECT_SQL[kind],
                    (snapshot_id,),
                ).fetchone(),
            )
            if row is None or str(_column(row, "payload_json")) != payload_json:
                raise MalformedSnapshotRecordError("snapshot ID is already bound to different content")

    def _get(self, kind: _SnapshotKind, snapshot_id: str) -> str:
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    _SELECT_SQL[kind],
                    (snapshot_id,),
                ).fetchone(),
            )
        if row is None:
            raise SnapshotNotFoundError(f"snapshot not found: {snapshot_id}")
        return str(_column(row, "payload_json"))


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])
