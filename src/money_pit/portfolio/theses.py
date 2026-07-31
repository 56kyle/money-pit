"""Module containing durable living-thesis state and transition history."""

import json
import sqlite3
from datetime import datetime
from datetime import timezone
from typing import Final
from typing import cast

from pydantic import ValidationError

from money_pit.schemas.theses import ScenarioOutcome
from money_pit.schemas.theses import Thesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.theses import ThesisStatus
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


_ALLOWED_TRANSITIONS: Final[dict[ThesisStatus, frozenset[ThesisStatus]]] = {
    ThesisStatus.CANDIDATE: frozenset({ThesisStatus.ACTIVE, ThesisStatus.INVALIDATED, ThesisStatus.CLOSED}),
    ThesisStatus.ACTIVE: frozenset({ThesisStatus.WEAKENED, ThesisStatus.INVALIDATED, ThesisStatus.CLOSED}),
    ThesisStatus.WEAKENED: frozenset({ThesisStatus.ACTIVE, ThesisStatus.INVALIDATED, ThesisStatus.CLOSED}),
    ThesisStatus.INVALIDATED: frozenset({ThesisStatus.CLOSED}),
    ThesisStatus.CLOSED: frozenset(),
}


class ThesisRepositoryError(StorageError):
    """Base class for invalid durable thesis state."""


class ThesisNotFoundError(ThesisRepositoryError):
    """Raised when a requested thesis is absent."""


class ThesisAlreadyExistsError(ThesisRepositoryError):
    """Raised when append would replace an existing thesis."""


class InvalidThesisTransitionError(ThesisRepositoryError):
    """Raised when a requested status transition is not allowed."""


class MalformedThesisRecordError(ThesisRepositoryError):
    """Raised when durable thesis state violates its schema."""


class ThesisRepository:
    """Append theses, transition status, and query point-in-time relevance."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to an initialized database."""
        self._database: Database = database

    def append(self, thesis: Thesis) -> None:
        """Append a new thesis and its initial status-history record."""
        instrument_or_theme: str = _encode_subject(thesis)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            existing: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT thesis_id FROM theses WHERE thesis_id = ?",
                    (thesis.thesis_id,),
                ).fetchone(),
            )
            if existing is not None:
                raise ThesisAlreadyExistsError(f"thesis already exists: {thesis.thesis_id}")
            _insert_thesis(connection, thesis, instrument_or_theme)
            _insert_history(connection, thesis, prior_status=None)

    def get(self, thesis_id: str) -> Thesis:
        """Return one durable thesis."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT * FROM theses WHERE thesis_id = ?",
                    (thesis_id,),
                ).fetchone(),
            )
        if row is None:
            raise ThesisNotFoundError(f"thesis not found: {thesis_id}")
        return _thesis_from_row(row)

    def transition(
        self,
        thesis_id: str,
        *,
        next_status: ThesisStatus,
        reviewed_at: datetime,
        expires_at: datetime | None = None,
    ) -> Thesis:
        """Apply one allowed status transition and append its history atomically."""
        _require_aware(reviewed_at)
        if expires_at is not None:
            _require_aware(expires_at)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT * FROM theses WHERE thesis_id = ?",
                    (thesis_id,),
                ).fetchone(),
            )
            if row is None:
                raise ThesisNotFoundError(f"thesis not found: {thesis_id}")
            history_row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                    SELECT thesis_id, next_status, transitioned_at, thesis_json
                    FROM thesis_status_history
                    WHERE thesis_id = ?
                    ORDER BY transitioned_at DESC, transition_id DESC
                    LIMIT 1
                    """,
                    (thesis_id,),
                ).fetchone(),
            )
            if history_row is None:
                raise MalformedThesisRecordError("stored thesis has no status history")
            current: Thesis = _thesis_from_history_row(history_row)
            latest_transitioned_at: datetime = _datetime_from_database(_column(history_row, "transitioned_at"))
            if reviewed_at <= latest_transitioned_at:
                raise InvalidThesisTransitionError(
                    "thesis transition review time must be later than its latest history record"
                )
            if next_status not in _ALLOWED_TRANSITIONS[current.status]:
                raise InvalidThesisTransitionError(
                    f"cannot transition thesis from {current.status.value} to {next_status.value}"
                )
            transitioned: Thesis = current.model_copy(
                update={
                    "status": next_status,
                    "reviewed_at": reviewed_at,
                    "expires_at": expires_at if expires_at is not None else current.expires_at,
                }
            )
            _ = connection.execute(
                """
                UPDATE theses
                SET status = ?, reviewed_at = ?, expires_at = ?
                WHERE thesis_id = ?
                """,
                (
                    transitioned.status.value,
                    _utc_text(transitioned.reviewed_at),
                    (_utc_text(transitioned.expires_at) if transitioned.expires_at is not None else None),
                    transitioned.thesis_id,
                ),
            )
            _insert_history(connection, transitioned, prior_status=current.status)
        return transitioned

    def active(self, *, as_of: datetime) -> tuple[Thesis, ...]:
        """Return active, unexpired theses in stable identifier order."""
        _require_aware(as_of)
        return tuple(
            thesis
            for thesis in self._history_as_of(as_of)
            if thesis.status is ThesisStatus.ACTIVE and (thesis.expires_at is None or thesis.expires_at > as_of)
        )

    def expired(self, *, as_of: datetime) -> tuple[Thesis, ...]:
        """Return theses whose declared expiry is at or before the point in time."""
        _require_aware(as_of)
        return tuple(
            thesis
            for thesis in self._history_as_of(as_of)
            if thesis.expires_at is not None and thesis.expires_at <= as_of
        )

    def _history_as_of(self, as_of: datetime) -> tuple[Thesis, ...]:
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = connection.execute(
                """
                SELECT history.thesis_id, history.next_status,
                       history.transitioned_at, history.thesis_json
                FROM thesis_status_history AS history
                WHERE history.transition_id = (
                    SELECT candidate.transition_id
                    FROM thesis_status_history AS candidate
                    WHERE candidate.thesis_id = history.thesis_id
                      AND candidate.transitioned_at <= ?
                    ORDER BY candidate.transitioned_at DESC,
                             candidate.transition_id DESC
                    LIMIT 1
                )
                ORDER BY history.thesis_id
                """,
                (_utc_text(as_of),),
            ).fetchall()
        return tuple(_thesis_from_history_row(row) for row in rows)


def _encode_subject(thesis: Thesis) -> str:
    if (thesis.instrument is None) == (thesis.theme is None):
        raise MalformedThesisRecordError("a thesis must identify exactly one instrument or theme")
    if thesis.instrument is not None:
        return f"instrument:{thesis.instrument}"
    return f"theme:{thesis.theme}"


def _insert_thesis(
    connection: sqlite3.Connection,
    thesis: Thesis,
    instrument_or_theme: str,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO theses (
            thesis_id, instrument_or_theme, direction, horizon, status,
            supporting_claim_keys_json, contradicting_claim_keys_json,
            scenario_distribution_json, invalidation_rules_json, confidence,
            created_at, reviewed_at, expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thesis.thesis_id,
            instrument_or_theme,
            thesis.direction.value,
            thesis.horizon,
            thesis.status.value,
            json.dumps(thesis.supporting_claim_keys),
            json.dumps(thesis.contradicting_claim_keys),
            json.dumps([scenario.model_dump(mode="json") for scenario in thesis.scenario_distribution]),
            json.dumps(thesis.invalidation_rules),
            thesis.confidence,
            _utc_text(thesis.created_at),
            _utc_text(thesis.reviewed_at),
            _utc_text(thesis.expires_at) if thesis.expires_at is not None else None,
        ),
    )


def _insert_history(
    connection: sqlite3.Connection,
    thesis: Thesis,
    *,
    prior_status: ThesisStatus | None,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO thesis_status_history (
            thesis_id, prior_status, next_status, transitioned_at, thesis_json
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            thesis.thesis_id,
            prior_status.value if prior_status is not None else None,
            thesis.status.value,
            _utc_text(thesis.reviewed_at),
            thesis.model_dump_json(),
        ),
    )


def _thesis_from_row(row: sqlite3.Row) -> Thesis:
    try:
        subject: str = str(_column(row, "instrument_or_theme"))
        instrument: str | None
        theme: str | None
        if subject.startswith("instrument:"):
            instrument = subject.removeprefix("instrument:")
            theme = None
        elif subject.startswith("theme:"):
            instrument = None
            theme = subject.removeprefix("theme:")
        else:
            raise ValueError("stored thesis subject has no type prefix")
        return Thesis(
            thesis_id=str(_column(row, "thesis_id")),
            instrument=instrument,
            theme=theme,
            direction=ThesisDirection(str(_column(row, "direction"))),
            horizon=str(_column(row, "horizon")),
            status=ThesisStatus(str(_column(row, "status"))),
            supporting_claim_keys=_json_string_tuple(_column(row, "supporting_claim_keys_json")),
            contradicting_claim_keys=_json_string_tuple(_column(row, "contradicting_claim_keys_json")),
            scenario_distribution=tuple(
                ScenarioOutcome.model_validate(item)
                for item in _json_object_list(_column(row, "scenario_distribution_json"))
            ),
            invalidation_rules=_json_string_tuple(_column(row, "invalidation_rules_json")),
            confidence=_number_from_database(_column(row, "confidence")),
            created_at=_datetime_from_database(_column(row, "created_at")),
            reviewed_at=_datetime_from_database(_column(row, "reviewed_at")),
            expires_at=_optional_datetime_from_database(_column(row, "expires_at")),
        )
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
        raise MalformedThesisRecordError("stored thesis is malformed") from error


def _thesis_from_history_row(row: sqlite3.Row) -> Thesis:
    try:
        thesis_json: object = _column(row, "thesis_json")
        if not isinstance(thesis_json, str):
            raise TypeError("stored thesis history snapshot must be text")
        thesis: Thesis = Thesis.model_validate_json(thesis_json)
        if thesis.thesis_id != str(_column(row, "thesis_id")):
            raise ValueError("stored thesis history identifier does not match its snapshot")
        if thesis.status.value != str(_column(row, "next_status")):
            raise ValueError("stored thesis history status does not match its snapshot")
        transitioned_at: datetime = _datetime_from_database(_column(row, "transitioned_at"))
        if thesis.reviewed_at != transitioned_at:
            raise ValueError("stored thesis history time does not match its snapshot")
        return thesis
    except (TypeError, ValueError, ValidationError) as error:
        raise MalformedThesisRecordError("stored thesis history is malformed") from error


def _json_string_tuple(value: object) -> tuple[str, ...]:
    parsed: object = cast("object", json.loads(str(value)))
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise TypeError("stored JSON value must be an array of text")
    return tuple(cast("list[str]", parsed))


def _json_object_list(value: object) -> tuple[dict[str, object], ...]:
    parsed: object = cast("object", json.loads(str(value)))
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise TypeError("stored scenario distribution must be an array of objects")
    return tuple(cast("list[dict[str, object]]", parsed))


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])


def _number_from_database(value: object) -> float:
    if not isinstance(value, (int, float)):
        raise TypeError("stored number must be numeric")
    return float(value)


def _datetime_from_database(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("stored datetime must be text")
    parsed: datetime = datetime.fromisoformat(value)
    _require_aware(parsed)
    return parsed


def _optional_datetime_from_database(value: object) -> datetime | None:
    return None if value is None else _datetime_from_database(value)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def _utc_text(value: datetime) -> str:
    _require_aware(value)
    return value.astimezone(timezone.utc).isoformat()
