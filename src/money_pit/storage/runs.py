"""Module containing durable run and stage-artifact persistence."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import cast

from pydantic import JsonValue
from pydantic import TypeAdapter
from pydantic import ValidationError

from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import RunTerminalEvent
from money_pit.schemas.runs import StageArtifactRecord
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


if TYPE_CHECKING:
    import sqlite3


class RunRepositoryError(StorageError):
    """Base class for invalid durable run state."""


class ImmutableRunCollisionError(RunRepositoryError):
    """Raised when a run or artifact identity is reused for different content."""


class RunNotFoundError(RunRepositoryError):
    """Raised when an artifact references an unknown run."""


class MalformedRunRecordError(RunRepositoryError):
    """Raised when stored run state violates its typed contract."""


class InvalidRunTerminalEventError(RunRepositoryError):
    """Raised when a terminal event predates its immutable run start."""


class InvalidStageArtifactError(RunRepositoryError):
    """Raised when an artifact violates its immutable run lifecycle."""


class RunRepository:
    """Persist runs and authoritative same-run record bindings."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to one initialized database."""
        self._database: Database = database

    def append_run(self, run: RunRecord) -> None:
        """Append one run with actual, non-backdated lifecycle timestamps."""
        encoded: str = run.canonical_json()
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """
                INSERT OR IGNORE INTO runs (
                    run_id, requested_as_of, started_at, known_at, through_stage,
                    source_config_hash, intelligence_config_hash, portfolio_config_hash,
                    execution_config_hash, manifest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.requested_as_of.isoformat(),
                    run.started_at.isoformat(),
                    run.known_at.isoformat(),
                    run.through_stage,
                    run.source_config_hash,
                    run.intelligence_config_hash,
                    run.portfolio_config_hash,
                    run.execution_config_hash,
                    encoded,
                ),
            )
            row = cast(
                "sqlite3.Row | None",
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (run.run_id,)).fetchone(),
            )
            if row is None or _run_from_row(row) != run:
                raise ImmutableRunCollisionError(f"Run ID {run.run_id!r} is bound to different content.")

    def append_terminal_event(self, event: RunTerminalEvent) -> None:
        """Append the one exact terminal event for a started run idempotently."""
        encoded = event.canonical_json()
        with self._database.transaction(TransactionMode.WRITE) as connection:
            run_row = cast(
                "sqlite3.Row | None",
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (event.run_id,)).fetchone(),
            )
            if run_row is None:
                raise RunNotFoundError(f"Run not found: {event.run_id}")
            run = _run_from_row(run_row)
            if event.completed_at < run.started_at:
                raise InvalidRunTerminalEventError("Run completion must not precede its start.")
            failure_detail_json = (
                None
                if event.failure_detail is None
                else json.dumps(
                    event.failure_detail.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            _ = connection.execute(
                """
                INSERT OR IGNORE INTO run_terminal_events (
                    run_id, status, completed_at, known_at, failure_kind,
                    failure_detail_json, event_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.status.value,
                    event.completed_at.isoformat(),
                    event.known_at.isoformat(),
                    event.failure_kind,
                    failure_detail_json,
                    encoded,
                ),
            )
            stored = _selected_json(
                connection,
                "run_terminal_events",
                "run_id",
                event.run_id,
                "event_json",
            )
            if stored != encoded:
                raise ImmutableRunCollisionError(f"Run {event.run_id!r} already has a different terminal event.")

    def terminal_event_for_run(self, run_id: str) -> RunTerminalEvent | None:
        """Return the terminal event, or None while the run remains nonterminal."""
        with self._database.transaction() as connection:
            serialized = _selected_json(
                connection,
                "run_terminal_events",
                "run_id",
                run_id,
                "event_json",
            )
        if serialized is None:
            return None
        try:
            return RunTerminalEvent.model_validate_json(serialized)
        except (ValueError, ValidationError) as error:
            raise MalformedRunRecordError("Stored run terminal event is malformed.") from error

    def append_stage_artifact(self, artifact: StageArtifactRecord) -> None:
        """Append one stage artifact and its exact same-run input/output bindings."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            append_stage_artifact_record(connection, artifact)

    def output_ids_for_run(self, run_id: str) -> tuple[str, ...]:
        """Return exact same-run record IDs in authoritative stage order."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = connection.execute(
                """
                SELECT output_ids_json FROM stage_artifacts
                WHERE run_id = ? ORDER BY stage, known_at, artifact_id
                """,
                (run_id,),
            ).fetchall()
        try:
            return tuple(identifier for row in rows for identifier in _string_tuple(_column(row, "output_ids_json")))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise MalformedRunRecordError("Stored stage output IDs are malformed.") from error

    def get_run(self, run_id: str) -> RunRecord:
        """Return one exact durable run record."""
        run = self.find_run(run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        return run

    def find_run(self, run_id: str) -> RunRecord | None:
        """Return an exact durable run record when registered."""
        with self._database.transaction() as connection:
            row = cast(
                "sqlite3.Row | None",
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone(),
            )
        if row is None:
            return None
        return _run_from_row(row)

    def artifacts_for_run(self, run_id: str) -> tuple[StageArtifactRecord, ...]:
        """Return exact stage artifacts without consulting current providers."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = connection.execute(
                "SELECT * FROM stage_artifacts WHERE run_id = ? ORDER BY stage, known_at, artifact_id",
                (run_id,),
            ).fetchall()
        return tuple(_artifact_from_row(row) for row in rows)


def append_stage_artifact_record(
    connection: sqlite3.Connection,
    artifact: StageArtifactRecord,
) -> None:
    """Append one lifecycle-validated artifact inside an existing write transaction."""
    payload_json = json.dumps(
        artifact.payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    run_row = cast(
        "sqlite3.Row | None",
        connection.execute("SELECT * FROM runs WHERE run_id = ?", (artifact.run_id,)).fetchone(),
    )
    if run_row is None:
        raise RunNotFoundError(f"Run not found: {artifact.run_id}")
    run = _run_from_row(run_row)
    if (
        connection.execute(
            "SELECT 1 FROM run_terminal_events WHERE run_id = ?",
            (artifact.run_id,),
        ).fetchone()
        is not None
    ):
        raise InvalidStageArtifactError("A terminal run cannot accept another stage artifact.")
    if artifact.requested_as_of != run.requested_as_of:
        raise InvalidStageArtifactError("Artifact cutoff differs from its immutable run.")
    if artifact.started_at < run.started_at:
        raise InvalidStageArtifactError("Artifact start predates its immutable run.")
    if int(artifact.stage[1]) > int(run.through_stage[1]):
        raise InvalidStageArtifactError("Artifact stage exceeds the run terminal stage.")
    _ = connection.execute(
        """
        INSERT OR IGNORE INTO stage_artifacts (
            artifact_id, run_id, stage, requested_as_of, started_at,
            decision_at, known_at, input_ids_json, output_ids_json,
            implementation_version, payload_hash, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact.artifact_id,
            artifact.run_id,
            artifact.stage,
            artifact.requested_as_of.isoformat(),
            artifact.started_at.isoformat(),
            artifact.decision_at.isoformat() if artifact.decision_at is not None else None,
            artifact.known_at.isoformat(),
            json.dumps(artifact.input_ids),
            json.dumps(artifact.output_ids),
            artifact.implementation_version,
            artifact.payload_hash(),
            payload_json,
        ),
    )
    row = cast(
        "sqlite3.Row | None",
        connection.execute(
            "SELECT * FROM stage_artifacts WHERE artifact_id = ?",
            (artifact.artifact_id,),
        ).fetchone(),
    )
    if row is None or _artifact_from_row(row) != artifact or _text(row, "payload_hash") != artifact.payload_hash():
        raise ImmutableRunCollisionError(f"Stage artifact ID {artifact.artifact_id!r} is bound to different content.")


def _artifact_from_row(row: sqlite3.Row) -> StageArtifactRecord:
    try:
        payload_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
        payload: JsonValue = payload_adapter.validate_json(_text(row, "payload_json"))
        values: dict[str, object] = {
            "artifact_id": _text(row, "artifact_id"),
            "run_id": _text(row, "run_id"),
            "stage": _text(row, "stage"),
            "requested_as_of": _text(row, "requested_as_of"),
            "started_at": _text(row, "started_at"),
            "decision_at": _optional_text(row, "decision_at"),
            "known_at": _text(row, "known_at"),
            "input_ids": _string_tuple(_column(row, "input_ids_json")),
            "output_ids": _string_tuple(_column(row, "output_ids_json")),
            "implementation_version": _text(row, "implementation_version"),
            "payload": payload,
        }
        return StageArtifactRecord.model_validate(values)
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
        raise MalformedRunRecordError("Stored stage artifact is malformed.") from error


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    try:
        run = RunRecord.model_validate_json(_text(row, "manifest_json"))
        scalar_values = (
            _text(row, "run_id"),
            _text(row, "requested_as_of"),
            _text(row, "started_at"),
            _text(row, "known_at"),
            _text(row, "through_stage"),
            _text(row, "source_config_hash"),
            _optional_text(row, "intelligence_config_hash"),
            _optional_text(row, "portfolio_config_hash"),
            _optional_text(row, "execution_config_hash"),
        )
        record_values = (
            run.run_id,
            run.requested_as_of.isoformat(),
            run.started_at.isoformat(),
            run.known_at.isoformat(),
            run.through_stage,
            run.source_config_hash,
            run.intelligence_config_hash,
            run.portfolio_config_hash,
            run.execution_config_hash,
        )
        if scalar_values != record_values:
            raise MalformedRunRecordError("Stored run columns disagree with its manifest JSON.")
        return run
    except MalformedRunRecordError:
        raise
    except (TypeError, ValueError, ValidationError) as error:
        raise MalformedRunRecordError("Stored run record is malformed.") from error


def _selected_json(
    connection: sqlite3.Connection, table: str, id_column: str, identifier: str, json_column: str
) -> str | None:
    # Table and column identifiers are selected by the replay validator.
    query = f"SELECT {json_column} FROM {table} WHERE {id_column} = ?"  # noqa: S608  # nosec B608
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute(query, (identifier,)).fetchone(),
    )
    return None if row is None else _text(row, json_column)


def _string_tuple(value: object) -> tuple[str, ...]:
    parsed: object = cast("object", json.loads(str(value)))
    if not isinstance(parsed, list):
        raise TypeError("Stored identifier JSON must be an array of strings.")
    values: list[str] = []
    for item in cast("list[object]", parsed):
        if not isinstance(item, str):
            raise TypeError("Stored identifier JSON must be an array of strings.")
        values.append(item)
    return tuple(values)


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])


def _text(row: sqlite3.Row, name: str) -> str:
    value: object = _column(row, name)
    if not isinstance(value, str):
        raise TypeError(f"Stored {name} must be text.")
    return value


def _optional_text(row: sqlite3.Row, name: str) -> str | None:
    value: object = _column(row, name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Stored {name} must be text or null.")
    return value
