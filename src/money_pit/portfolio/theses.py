"""Module containing append-only candidate and thesis-revision persistence."""

import json
import sqlite3
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from money_pit.schemas.temporal import SignalContribution
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisRevision
from money_pit.schemas.theses import ThesisStatus
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


class ThesisRepositoryError(StorageError):
    """Base class for invalid durable thesis state."""


class ImmutableThesisCollisionError(ThesisRepositoryError):
    """Raised when a candidate or revision identity is reused for different content."""


class InvalidThesisRevisionError(ThesisRepositoryError):
    """Raised when a revision does not extend the exact prior thesis history."""


class MalformedThesisRecordError(ThesisRepositoryError):
    """Raised when durable thesis JSON or indexed metadata is inconsistent."""


class ThesisRepository:
    """Persist sourced candidates and complete immutable thesis revisions."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to an initialized database."""
        self._database: Database = database

    def append_candidate(self, candidate: CandidateThesis) -> None:
        """Persist one sourced candidate idempotently without mutating its status."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """
                INSERT INTO candidate_theses (
                    candidate_thesis_id, status, created_at, known_at, candidate_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    candidate.candidate_thesis_id,
                    candidate.status.value,
                    candidate.created_at.isoformat(),
                    candidate.known_at.isoformat(),
                    candidate.model_dump_json(),
                ),
            )
            stored: CandidateThesis | None = _select_candidate(connection, candidate.candidate_thesis_id)
            if stored != candidate:
                raise ImmutableThesisCollisionError("candidate identity is already bound to different content")

    def append_revision(self, revision: ThesisRevision) -> None:
        """Append one complete revision after validating exact sequence and promotion."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            append_revision_record(connection, revision)

    def append_contribution(self, contribution: SignalContribution) -> None:
        """Persist one immutable temporal contribution idempotently."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            append_contribution_record(connection, contribution)

    def get_candidate(self, candidate_thesis_id: str) -> CandidateThesis | None:
        """Return one immutable candidate when present."""
        with self._database.transaction() as connection:
            return _select_candidate(connection, candidate_thesis_id)

    def get_revision(self, revision_id: str) -> ThesisRevision | None:
        """Return one immutable revision when present."""
        with self._database.transaction() as connection:
            return _select_revision(connection, revision_id)

    def candidates_by_ids(self, candidate_ids: tuple[str, ...]) -> tuple[CandidateThesis, ...]:
        """Return the exact requested candidates or fail on an unknown identity."""
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ThesisRepositoryError("candidate identities must be unique")
        with self._database.transaction() as connection:
            candidates: tuple[CandidateThesis | None, ...] = tuple(
                _select_candidate(connection, candidate_id) for candidate_id in candidate_ids
            )
        if any(candidate is None for candidate in candidates):
            raise ThesisRepositoryError("one or more requested candidate identities are unknown")
        return tuple(cast("CandidateThesis", candidate) for candidate in candidates)

    def revisions_by_ids(self, revision_ids: tuple[str, ...]) -> tuple[ThesisRevision, ...]:
        """Return the exact requested revisions or fail on an unknown identity."""
        if len(set(revision_ids)) != len(revision_ids):
            raise ThesisRepositoryError("revision identities must be unique")
        with self._database.transaction() as connection:
            revisions: tuple[ThesisRevision | None, ...] = tuple(
                _select_revision(connection, revision_id) for revision_id in revision_ids
            )
        if any(revision is None for revision in revisions):
            raise ThesisRepositoryError("one or more requested revision identities are unknown")
        return tuple(cast("ThesisRevision", revision) for revision in revisions)

    def _revisions_as_of(self, *, as_of: datetime) -> tuple[ThesisRevision, ...]:
        """Return the last knowable revision of every thesis at a historical cutoff."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """
                    SELECT revision_json, revision_id, thesis_id, revision_number, status,
                           created_at, known_at, review_at, valid_until
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY thesis_id
                            ORDER BY known_at DESC, revision_number DESC, revision_id DESC
                        ) AS historical_rank
                        FROM thesis_revisions
                        WHERE known_at <= ?
                    )
                    WHERE historical_rank = 1
                    ORDER BY thesis_id
                    """,
                    (as_of.isoformat(),),
                ).fetchall(),
            )
        return tuple(_revision_from_row(row) for row in rows)

    def candidates_as_of(self, *, as_of: datetime) -> tuple[CandidateThesis, ...]:
        """Return candidates whose durable knowledge time is within the cutoff."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """
                    SELECT * FROM candidate_theses
                    WHERE known_at <= ?
                    ORDER BY known_at, candidate_thesis_id
                    """,
                    (as_of.isoformat(),),
                ).fetchall(),
            )
        return tuple(_candidate_from_row(row) for row in rows)

    def candidates_due_for_research(
        self,
        *,
        as_of: datetime,
        exact_candidate_ids: tuple[str, ...] = (),
    ) -> tuple[CandidateThesis, ...]:
        """Return every eligible candidate in deterministic least-recently-researched order."""
        if len(exact_candidate_ids) != len(set(exact_candidate_ids)):
            raise ThesisRepositoryError("candidate identities must be unique")
        if exact_candidate_ids:
            _ = self.candidates_by_ids(exact_candidate_ids)
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """
                    SELECT candidate.*,
                           (
                               SELECT MIN(task.known_at)
                               FROM planned_research_tasks AS task
                               WHERE task.candidate_thesis_id = candidate.candidate_thesis_id
                                 AND task.status = 'pending'
                           ) AS oldest_pending_at,
                           (
                               SELECT MAX(session.started_at)
                               FROM research_sessions AS session
                               WHERE session.scope_kind = 'candidate_thesis'
                                 AND session.scope_subject_id = candidate.candidate_thesis_id
                           ) AS last_researched_at
                    FROM candidate_theses AS candidate
                    WHERE candidate.status IN ('open', 'researching', 'unresolved')
                      AND (
                          candidate.known_at <= ?
                          OR candidate.candidate_thesis_id IN (
                              SELECT value FROM json_each(?)
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM theses
                          WHERE theses.candidate_thesis_id = candidate.candidate_thesis_id
                      )
                    ORDER BY
                        CASE WHEN oldest_pending_at IS NULL THEN 1 ELSE 0 END,
                        oldest_pending_at,
                        CASE WHEN last_researched_at IS NULL THEN 0 ELSE 1 END,
                        last_researched_at,
                        candidate.known_at,
                        candidate.candidate_thesis_id
                    """,
                    (as_of.isoformat(), json.dumps(exact_candidate_ids)),
                ).fetchall(),
            )
        return tuple(_candidate_from_row(row) for row in rows)

    def revisions_as_of(self, *, as_of: datetime) -> tuple[ThesisRevision, ...]:
        """Return the last knowable complete revision for every thesis."""
        return self._revisions_as_of(as_of=as_of)

    def active_as_of(self, *, as_of: datetime) -> tuple[ThesisRevision, ...]:
        """Return active, unexpired revisions knowable at the cutoff."""
        return tuple(
            revision
            for revision in self.revisions_as_of(as_of=as_of)
            if revision.status is ThesisStatus.ACTIVE and (revision.valid_until is None or revision.valid_until > as_of)
        )

    @staticmethod
    def require_revision_sequence(connection: sqlite3.Connection, revision: ThesisRevision) -> None:
        """Require an append-only revision number following the current head."""
        row: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                """
                SELECT revision_id, revision_number
                FROM thesis_revisions
                WHERE thesis_id = ?
                ORDER BY revision_number DESC
                LIMIT 1
                """,
                (revision.thesis_id,),
            ).fetchone(),
        )
        if row is None and revision.revision_number != 1:
            raise InvalidThesisRevisionError("a thesis must begin at revision one")
        if row is not None and revision.revision_number != _integer(row, "revision_number") + 1:
            existing: ThesisRevision | None = _select_revision(connection, revision.revision_id)
            if existing != revision:
                raise InvalidThesisRevisionError("revision number must exactly follow the current thesis revision")


def append_revision_record(
    connection: sqlite3.Connection,
    revision: ThesisRevision,
) -> None:
    """Append one validated thesis revision inside an existing write transaction."""
    existing = _select_revision(connection, revision.revision_id)
    if existing == revision:
        return
    if existing is not None:
        raise ImmutableThesisCollisionError("revision identity is already bound to different content")
    ThesisRepository.require_revision_sequence(connection, revision)
    if revision.revision_number == 1:
        candidate_id = revision.promoted_from_candidate_id or ""
        if _select_candidate(connection, candidate_id) is None:
            raise InvalidThesisRevisionError("first revision references an unknown candidate")
        _ = connection.execute(
            """
            INSERT INTO theses (thesis_id, candidate_thesis_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (revision.thesis_id, candidate_id, revision.created_at.isoformat()),
        )
    _ = connection.execute(
        """
        INSERT INTO thesis_revisions (
            revision_id, thesis_id, revision_number, status, created_at,
            known_at, review_at, valid_until, revision_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            revision.revision_id,
            revision.thesis_id,
            revision.revision_number,
            revision.status.value,
            revision.created_at.isoformat(),
            revision.known_at.isoformat(),
            revision.review_at.isoformat(),
            None if revision.valid_until is None else revision.valid_until.isoformat(),
            revision.model_dump_json(),
        ),
    )
    if _select_revision(connection, revision.revision_id) != revision:
        raise ImmutableThesisCollisionError("revision identity or number is already bound to different content")


def append_contribution_record(
    connection: sqlite3.Connection,
    contribution: SignalContribution,
) -> None:
    """Append one temporal contribution inside an existing write transaction."""
    _ = connection.execute(
        """
        INSERT INTO signal_contributions (
            contribution_id, observation_id, thesis_revision_id, relation,
            temporal_compatible, judged_at, known_at, contribution_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            contribution.contribution_id,
            contribution.observation_id,
            contribution.thesis_revision_id,
            contribution.relation.value,
            int(contribution.temporal_compatible),
            contribution.judged_at.isoformat(),
            contribution.known_at.isoformat(),
            contribution.model_dump_json(),
        ),
    )
    if _select_contribution(connection, contribution.contribution_id) != contribution:
        raise ImmutableThesisCollisionError("contribution identity is already bound to different content")


def _select_candidate(connection: sqlite3.Connection, candidate_id: str) -> CandidateThesis | None:
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute("SELECT * FROM candidate_theses WHERE candidate_thesis_id = ?", (candidate_id,)).fetchone(),
    )
    return None if row is None else _candidate_from_row(row)


def _select_revision(connection: sqlite3.Connection, revision_id: str) -> ThesisRevision | None:
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute("SELECT * FROM thesis_revisions WHERE revision_id = ?", (revision_id,)).fetchone(),
    )
    return None if row is None else _revision_from_row(row)


def _select_contribution(connection: sqlite3.Connection, contribution_id: str) -> SignalContribution | None:
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute(
            "SELECT * FROM signal_contributions WHERE contribution_id = ?", (contribution_id,)
        ).fetchone(),
    )
    return None if row is None else _contribution_from_row(row)


def _candidate_from_row(row: sqlite3.Row) -> CandidateThesis:
    try:
        candidate: CandidateThesis = CandidateThesis.model_validate_json(_text(row, "candidate_json"))
        indexed: tuple[tuple[object, object], ...] = (
            (_text(row, "candidate_thesis_id"), candidate.candidate_thesis_id),
            (_text(row, "status"), candidate.status.value),
            (_text(row, "created_at"), candidate.created_at.isoformat()),
            (_text(row, "known_at"), candidate.known_at.isoformat()),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise MalformedThesisRecordError("stored candidate thesis is malformed") from error
    if any(stored != canonical for stored, canonical in indexed):
        raise MalformedThesisRecordError("stored candidate metadata disagrees with its payload")
    return candidate


def _revision_from_row(row: sqlite3.Row) -> ThesisRevision:
    try:
        revision: ThesisRevision = ThesisRevision.model_validate_json(_text(row, "revision_json"))
        indexed: tuple[tuple[object, object], ...] = (
            (_text(row, "revision_id"), revision.revision_id),
            (_text(row, "thesis_id"), revision.thesis_id),
            (_integer(row, "revision_number"), revision.revision_number),
            (_text(row, "status"), revision.status.value),
            (_text(row, "created_at"), revision.created_at.isoformat()),
            (_text(row, "known_at"), revision.known_at.isoformat()),
            (_text(row, "review_at"), revision.review_at.isoformat()),
            (
                _nullable_text(row, "valid_until"),
                None if revision.valid_until is None else revision.valid_until.isoformat(),
            ),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise MalformedThesisRecordError("stored thesis revision is malformed") from error
    if any(stored != canonical for stored, canonical in indexed):
        raise MalformedThesisRecordError("stored thesis-revision metadata disagrees with its payload")
    return revision


def _contribution_from_row(row: sqlite3.Row) -> SignalContribution:
    try:
        contribution: SignalContribution = SignalContribution.model_validate_json(_text(row, "contribution_json"))
        indexed: tuple[tuple[object, object], ...] = (
            (_text(row, "contribution_id"), contribution.contribution_id),
            (_text(row, "observation_id"), contribution.observation_id),
            (_text(row, "thesis_revision_id"), contribution.thesis_revision_id),
            (_text(row, "relation"), contribution.relation.value),
            (_integer(row, "temporal_compatible"), int(contribution.temporal_compatible)),
            (_text(row, "judged_at"), contribution.judged_at.isoformat()),
            (_text(row, "known_at"), contribution.known_at.isoformat()),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise MalformedThesisRecordError("stored signal contribution is malformed") from error
    if any(stored != canonical for stored, canonical in indexed):
        raise MalformedThesisRecordError("stored contribution metadata disagrees with its payload")
    return contribution


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


def _integer(row: sqlite3.Row, name: str) -> int:
    value: object = cast("object", row[name])
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"stored {name} must be an integer")
    return value
