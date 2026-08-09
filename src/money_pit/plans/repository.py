"""Module containing immutable portfolio-plan persistence.

Optimization results do not contain plan expiry, executable quantities, or gate
attestations. Callers must construct the complete ``PortfolioPlan`` before this
boundary will validate its digest and persist it.
"""

import sqlite3
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


class PortfolioPlanRepositoryError(StorageError):
    """Base class for invalid durable portfolio-plan state."""


class PortfolioPlanIntegrityError(PortfolioPlanRepositoryError):
    """Raised when an in-memory plan digest does not match its payload."""


class ImmutablePlanCollisionError(PortfolioPlanRepositoryError):
    """Raised when a plan identifier or digest is bound to different content."""


class MalformedPortfolioPlanRecordError(PortfolioPlanRepositoryError):
    """Raised when durable plan content or indexed metadata is inconsistent."""


class PortfolioPlanRepository:
    """Append and retrieve hash-validated immutable portfolio plans."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to an initialized database."""
        self._database: Database = database

    def append(self, plan: PortfolioPlan) -> None:
        """Persist one immutable, hash-validated plan idempotently."""
        if plan.plan_hash != plan.payload.sha256():
            raise PortfolioPlanIntegrityError("Portfolio plan payload does not match its digest")
        payload = plan.payload
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """
                INSERT INTO portfolio_plans (
                    plan_id, plan_hash, decision_snapshot_id, created_at, expires_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    payload.plan_id,
                    plan.plan_hash,
                    payload.decision_snapshot_id,
                    payload.created_at.isoformat(),
                    payload.expires_at.isoformat(),
                    plan.model_dump_json(),
                ),
            )
            row: sqlite3.Row | None = _select_plan_row(connection, payload.plan_id)
            if row is None:
                raise ImmutablePlanCollisionError(
                    f"Plan hash {plan.plan_hash!r} is already bound to a different plan ID"
                )
            stored: PortfolioPlan = _plan_from_row(row)
            if stored != plan:
                raise ImmutablePlanCollisionError(f"Plan ID {payload.plan_id!r} is already bound to different content")

    def get(self, plan_id: str) -> PortfolioPlan | None:
        """Return a hash-validated plan, failing closed on inconsistent metadata."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = _select_plan_row(connection, plan_id)
        return _plan_from_row(row) if row is not None else None


def _select_plan_row(connection: sqlite3.Connection, plan_id: str) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        connection.execute(
            """
            SELECT plan_id, plan_hash, decision_snapshot_id, created_at, expires_at, payload_json
            FROM portfolio_plans
            WHERE plan_id = ?
            """,
            (plan_id,),
        ).fetchone(),
    )


def _plan_from_row(row: sqlite3.Row) -> PortfolioPlan:
    try:
        plan: PortfolioPlan = PortfolioPlan.model_validate_json(_text_column(row, "payload_json"))
        payload = plan.payload
        indexed_values: tuple[tuple[object, object], ...] = (
            (_text_column(row, "plan_id"), payload.plan_id),
            (_text_column(row, "plan_hash"), plan.plan_hash),
            (_text_column(row, "decision_snapshot_id"), payload.decision_snapshot_id),
            (_datetime_column(row, "created_at"), payload.created_at),
            (_datetime_column(row, "expires_at"), payload.expires_at),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise MalformedPortfolioPlanRecordError("Stored portfolio plan is malformed") from error
    if any(indexed != canonical for indexed, canonical in indexed_values):
        raise MalformedPortfolioPlanRecordError("Stored portfolio-plan metadata disagrees with its payload")
    return plan


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])


def _text_column(row: sqlite3.Row, name: str) -> str:
    value: object = _column(row, name)
    if not isinstance(value, str):
        raise TypeError(f"Stored {name} must be text")
    return value


def _datetime_column(row: sqlite3.Row, name: str) -> datetime:
    parsed: datetime = datetime.fromisoformat(_text_column(row, name))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Stored {name} must be timezone-aware")
    return parsed
