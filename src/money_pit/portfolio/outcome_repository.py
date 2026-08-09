"""Module containing immutable scheduled-outcome persistence."""

import sqlite3
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from money_pit.schemas.outcomes import OutcomeMetric
from money_pit.schemas.outcomes import OutcomeSchedule
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


class OutcomeRepositoryError(StorageError):
    """Base class for invalid durable outcome state."""


class ImmutableOutcomeCollisionError(OutcomeRepositoryError):
    """Raised when an outcome identity is bound to different content."""


class MalformedOutcomeRecordError(OutcomeRepositoryError):
    """Raised when stored outcome content violates its typed contract."""


class SqliteOutcomeRepository:
    """Persist schedules and one immutable evaluation per schedule."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to an initialized database."""
        self._database: Database = database

    def append_schedule(self, schedule: OutcomeSchedule) -> None:
        """Persist one version-bound schedule idempotently."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """
                INSERT INTO outcome_schedules (
                    schedule_id, thesis_revision_id, plan_id, plan_hash,
                    benchmark_snapshot_id, boundary, observe_at, schedule_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    schedule.schedule_id,
                    schedule.thesis_revision_id,
                    schedule.plan_id,
                    schedule.plan_hash,
                    schedule.benchmark_snapshot_id,
                    schedule.boundary.value,
                    schedule.observe_at.isoformat(),
                    schedule.model_dump_json(),
                ),
            )
            stored: OutcomeSchedule | None = _select_schedule(connection, schedule.schedule_id)
            if stored != schedule:
                raise ImmutableOutcomeCollisionError("outcome schedule identity is already bound to different content")

    def due(self, *, as_of: datetime) -> tuple[OutcomeSchedule, ...]:
        """Return unevaluated schedules due at the trusted cutoff."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """
                    SELECT schedules.*
                    FROM outcome_schedules AS schedules
                    LEFT JOIN outcome_metrics AS metrics USING (schedule_id)
                    WHERE schedules.observe_at <= ? AND metrics.schedule_id IS NULL
                    ORDER BY schedules.observe_at, schedules.schedule_id
                    """,
                    (as_of.isoformat(),),
                ).fetchall(),
            )
        return tuple(_schedule_from_row(row) for row in rows)

    def append_metrics(self, metrics: OutcomeMetric) -> None:
        """Persist the single immutable evaluation for a schedule."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """
                INSERT INTO outcome_metrics (
                    metric_id, schedule_id, thesis_revision_id, plan_id, plan_hash,
                    benchmark_snapshot_id, evaluated_at, metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    metrics.metric_id,
                    metrics.schedule_id,
                    metrics.thesis_revision_id,
                    metrics.plan_id,
                    metrics.plan_hash,
                    metrics.benchmark_snapshot_id,
                    metrics.evaluated_at.isoformat(),
                    metrics.model_dump_json(),
                ),
            )
            stored: OutcomeMetric | None = _select_metrics(connection, metrics.metric_id)
            if stored != metrics:
                raise ImmutableOutcomeCollisionError("outcome metric identity is already bound to different content")


def _select_schedule(connection: sqlite3.Connection, schedule_id: str) -> OutcomeSchedule | None:
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute("SELECT * FROM outcome_schedules WHERE schedule_id = ?", (schedule_id,)).fetchone(),
    )
    return None if row is None else _schedule_from_row(row)


def _select_metrics(connection: sqlite3.Connection, metric_id: str) -> OutcomeMetric | None:
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute("SELECT * FROM outcome_metrics WHERE metric_id = ?", (metric_id,)).fetchone(),
    )
    if row is None:
        return None
    try:
        metrics: OutcomeMetric = OutcomeMetric.model_validate_json(_text(row, "metrics_json"))
    except (ValueError, ValidationError) as error:
        raise MalformedOutcomeRecordError("stored outcome metrics are malformed") from error
    indexed: tuple[tuple[object, object], ...] = (
        (_text(row, "metric_id"), metrics.metric_id),
        (_text(row, "schedule_id"), metrics.schedule_id),
        (_text(row, "thesis_revision_id"), metrics.thesis_revision_id),
        (_nullable_text(row, "plan_id"), metrics.plan_id),
        (_nullable_text(row, "plan_hash"), metrics.plan_hash),
        (_text(row, "benchmark_snapshot_id"), metrics.benchmark_snapshot_id),
        (_text(row, "evaluated_at"), metrics.evaluated_at.isoformat()),
    )
    if any(stored != canonical for stored, canonical in indexed):
        raise MalformedOutcomeRecordError("stored outcome metric metadata disagrees with its payload")
    return metrics


def _schedule_from_row(row: sqlite3.Row) -> OutcomeSchedule:
    try:
        schedule: OutcomeSchedule = OutcomeSchedule.model_validate_json(_text(row, "schedule_json"))
    except (ValueError, ValidationError) as error:
        raise MalformedOutcomeRecordError("stored outcome schedule is malformed") from error
    indexed: tuple[tuple[object, object], ...] = (
        (_text(row, "schedule_id"), schedule.schedule_id),
        (_text(row, "thesis_revision_id"), schedule.thesis_revision_id),
        (_nullable_text(row, "plan_id"), schedule.plan_id),
        (_nullable_text(row, "plan_hash"), schedule.plan_hash),
        (_text(row, "benchmark_snapshot_id"), schedule.benchmark_snapshot_id),
        (_text(row, "boundary"), schedule.boundary.value),
        (_text(row, "observe_at"), schedule.observe_at.isoformat()),
    )
    if any(stored != canonical for stored, canonical in indexed):
        raise MalformedOutcomeRecordError("stored outcome schedule metadata disagrees with its payload")
    return schedule


def _text(row: sqlite3.Row, name: str) -> str:
    value: object = cast("object", row[name])
    if not isinstance(value, str):
        raise TypeError(f"stored {name} must be text")
    return value


def _nullable_text(row: sqlite3.Row, name: str) -> str | None:
    value: object = cast("object", row[name])
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"stored {name} must be text or null")
