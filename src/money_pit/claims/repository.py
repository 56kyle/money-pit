"""Module containing durable claim history and projection repositories."""

# ruff: noqa: S608

import json
import sqlite3
from datetime import datetime
from datetime import timezone
from typing import cast

from pydantic import ValidationError

from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.projection import project_canonical_claim
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


class ClaimRepositoryError(StorageError):
    """Base class for invalid durable claim state."""


class ClaimNotFoundError(ClaimRepositoryError):
    """Raised when a requested canonical claim has no observations."""


class MalformedClaimRecordError(ClaimRepositoryError):
    """Raised when a durable claim row violates the domain schema."""


class ImmutableClaimCollisionError(ClaimRepositoryError):
    """Raised when an immutable claim identifier is bound to different content."""


class ClaimObservationNotFoundError(ClaimRepositoryError):
    """Raised when a verification references an unknown observation."""


class ClaimSourceItemNotFoundError(ClaimRepositoryError):
    """Raised when an observation references an unknown source item."""


class ClaimEvidenceFragmentNotFoundError(ClaimRepositoryError):
    """Raised when a claim or verification references an unknown evidence fragment."""


class ClaimEvidenceProvenanceError(ClaimRepositoryError):
    """Raised when claim evidence was not acquired from the observation source."""


class VerificationEvidenceNotFoundError(ClaimRepositoryError):
    """Raised when a verification references evidence that is not durable."""


class ClaimRepository:
    """Read immutable claim history and transactionally replace its projection."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to an initialized database."""
        self._database: Database = database

    def append_observation(self, observation: ClaimObservation) -> None:
        """Persist one immutable claim observation idempotently."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _require_observation_provenance(connection, observation)
            _ = connection.execute(
                """
                INSERT INTO claim_observations (
                    observation_id, canonical_claim_key, claim_text, claim_kind,
                    source_item_id, asserted_at, recorded_at, valid_from, horizon,
                    expires_at, supersedes_observation_id, subjects_json,
                    instruments_json, evidence_fragment_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO NOTHING
                """,
                (
                    observation.observation_id,
                    observation.canonical_claim_key,
                    observation.claim_text,
                    observation.claim_kind.value,
                    observation.source_item_id,
                    _utc_text(observation.asserted_at),
                    _utc_text(observation.recorded_at),
                    _optional_utc_text(observation.valid_from),
                    observation.horizon,
                    _optional_utc_text(observation.expires_at),
                    observation.supersedes_observation_id,
                    json.dumps(observation.subjects),
                    json.dumps(observation.instruments),
                    json.dumps(observation.evidence_fragment_ids),
                ),
            )
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                    SELECT observation_id, canonical_claim_key, claim_text, claim_kind,
                           subjects_json, instruments_json, source_item_id,
                           evidence_fragment_ids_json, asserted_at, recorded_at,
                           valid_from, horizon, expires_at, supersedes_observation_id
                    FROM claim_observations
                    WHERE observation_id = ?
                    """,
                    (observation.observation_id,),
                ).fetchone(),
            )
            if row is None or _observation_from_row(row) != observation:
                raise ImmutableClaimCollisionError(
                    f"Observation ID {observation.observation_id!r} is already bound to different content"
                )

    def append_verification(self, verification: VerificationResult) -> None:
        """Persist one immutable verification result idempotently."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            observation_exists: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT observation_id FROM claim_observations WHERE observation_id = ?",
                    (verification.observation_id,),
                ).fetchone(),
            )
            if observation_exists is None:
                raise ClaimObservationNotFoundError(f"Claim observation not found: {verification.observation_id}")
            _require_verification_evidence(connection, verification)
            _ = connection.execute(
                """
                INSERT INTO verification_results (
                    verification_id, observation_id, status,
                    supporting_evidence_ids_json, contradicting_evidence_ids_json,
                    checked_at, recorded_at, valid_until, verifier_version,
                    limitations_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(verification_id) DO NOTHING
                """,
                (
                    verification.verification_id,
                    verification.observation_id,
                    verification.status.value,
                    json.dumps(verification.supporting_evidence_ids),
                    json.dumps(verification.contradicting_evidence_ids),
                    _utc_text(verification.checked_at),
                    _utc_text(verification.recorded_at),
                    _optional_utc_text(verification.valid_until),
                    verification.verifier_version,
                    json.dumps(verification.limitations),
                ),
            )
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                    SELECT verification_id, observation_id, status,
                           supporting_evidence_ids_json, contradicting_evidence_ids_json,
                           checked_at, recorded_at, valid_until, verifier_version,
                           limitations_json
                    FROM verification_results
                    WHERE verification_id = ?
                    """,
                    (verification.verification_id,),
                ).fetchone(),
            )
            if row is None or _verification_from_row(row) != verification:
                raise ImmutableClaimCollisionError(
                    f"Verification ID {verification.verification_id!r} is already bound to different content"
                )

    def list_projections(self) -> tuple[CanonicalClaim, ...]:
        """Return every current claim projection in stable key order."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = connection.execute(
                """
                SELECT canonical_claim_key, current_status, active_observation_ids_json,
                       last_material_change_at, next_refresh_at
                FROM canonical_claims
                ORDER BY canonical_claim_key
                """
            ).fetchall()
        return tuple(_projection_from_row(row) for row in rows)

    def get_projection(self, canonical_claim_key: str) -> CanonicalClaim | None:
        """Return one current claim projection when it exists."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                SELECT canonical_claim_key, current_status, active_observation_ids_json,
                       last_material_change_at, next_refresh_at
                FROM canonical_claims
                WHERE canonical_claim_key = ?
                """,
                    (canonical_claim_key,),
                ).fetchone(),
            )
        return _projection_from_row(row) if row is not None else None

    def history(
        self,
        canonical_claim_key: str,
    ) -> tuple[tuple[ClaimObservation, ...], tuple[VerificationResult, ...]]:
        """Return immutable observations and their verification history."""
        with self._database.transaction() as connection:
            observations: tuple[ClaimObservation, ...] = _load_observations(
                connection,
                canonical_claim_key,
            )
            if not observations:
                raise ClaimNotFoundError(f"Claim not found: {canonical_claim_key}")
            verifications: tuple[VerificationResult, ...] = _load_verifications(
                connection,
                tuple(observation.observation_id for observation in observations),
            )
        return observations, verifications

    def replay(
        self,
        canonical_claim_key: str,
        *,
        as_of: datetime,
        policy: ClaimRefreshPolicy,
    ) -> CanonicalClaim:
        """Derive a historical projection without changing materialized current state."""
        _require_aware(as_of, field_name="as_of")
        with self._database.transaction() as connection:
            return _project_from_history(
                connection,
                canonical_claim_key,
                as_of=as_of,
                policy=policy,
            )

    def refresh(
        self,
        canonical_claim_key: str,
        *,
        as_of: datetime,
        policy: ClaimRefreshPolicy,
    ) -> CanonicalClaim:
        """Materialize one current projection at an operator-selected boundary."""
        _require_aware(as_of, field_name="as_of")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            projection: CanonicalClaim = _project_from_history(
                connection,
                canonical_claim_key,
                as_of=as_of,
                policy=policy,
            )
            _upsert_projection(connection, projection)
        return projection

    def refresh_all(
        self,
        *,
        as_of: datetime,
        policy: ClaimRefreshPolicy,
    ) -> tuple[CanonicalClaim, ...]:
        """Atomically replace current projections at one visibility boundary."""
        _require_aware(as_of, field_name="as_of")
        as_of_text: str = _utc_text(as_of)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            rows: list[sqlite3.Row] = connection.execute(
                """
                SELECT DISTINCT canonical_claim_key
                FROM claim_observations
                WHERE recorded_at <= ? AND asserted_at <= ?
                ORDER BY canonical_claim_key
                """,
                (as_of_text, as_of_text),
            ).fetchall()
            projections: tuple[CanonicalClaim, ...] = tuple(
                _project_from_history(
                    connection,
                    str(_column(row, "canonical_claim_key")),
                    as_of=as_of,
                    policy=policy,
                )
                for row in rows
            )
            _ = connection.execute("DELETE FROM canonical_claims")
            for projection in projections:
                _upsert_projection(connection, projection)
        return projections


def _require_observation_provenance(
    connection: sqlite3.Connection,
    observation: ClaimObservation,
) -> None:
    source_exists: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute(
            "SELECT source_item_id FROM source_items WHERE source_item_id = ? LIMIT 1",
            (observation.source_item_id,),
        ).fetchone(),
    )
    if source_exists is None:
        raise ClaimSourceItemNotFoundError(f"Claim source item not found: {observation.source_item_id}")
    for fragment_id in dict.fromkeys(observation.evidence_fragment_ids):
        fragment: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                "SELECT asset_id FROM evidence_fragments WHERE fragment_id = ?",
                (fragment_id,),
            ).fetchone(),
        )
        if fragment is None:
            raise ClaimEvidenceFragmentNotFoundError(f"Claim evidence fragment not found: {fragment_id}")
        acquisition: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                """
                SELECT acquisition_id
                FROM evidence_asset_acquisitions
                WHERE asset_id = ?
                  AND source_item_id = ?
                  AND provenance_status = 'resolved'
                LIMIT 1
                """,
                (_column(fragment, "asset_id"), observation.source_item_id),
            ).fetchone(),
        )
        if acquisition is None:
            message: str = (
                f"Claim evidence fragment {fragment_id!r} has no resolved acquisition "
                + f"from source item {observation.source_item_id!r}"
            )
            raise ClaimEvidenceProvenanceError(message)


def _require_verification_evidence(
    connection: sqlite3.Connection,
    verification: VerificationResult,
) -> None:
    evidence_ids: tuple[str, ...] = tuple(
        dict.fromkeys((*verification.supporting_evidence_ids, *verification.contradicting_evidence_ids))
    )
    for fragment_id in evidence_ids:
        row: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                "SELECT fragment_id FROM evidence_fragments WHERE fragment_id = ?",
                (fragment_id,),
            ).fetchone(),
        )
        if row is None:
            raise VerificationEvidenceNotFoundError(f"Verification evidence fragment not found: {fragment_id}")


def _project_from_history(
    connection: sqlite3.Connection,
    canonical_claim_key: str,
    *,
    as_of: datetime,
    policy: ClaimRefreshPolicy,
) -> CanonicalClaim:
    observations: tuple[ClaimObservation, ...] = _load_observations(
        connection,
        canonical_claim_key,
    )
    if not observations:
        raise ClaimNotFoundError(f"Claim not found: {canonical_claim_key}")
    verifications: tuple[VerificationResult, ...] = _load_verifications(
        connection,
        tuple(observation.observation_id for observation in observations),
    )
    return project_canonical_claim(
        observations,
        verifications,
        as_of=as_of,
        refresh_policy=policy,
    )


def _upsert_projection(
    connection: sqlite3.Connection,
    projection: CanonicalClaim,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO canonical_claims (
            canonical_claim_key, current_status, active_observation_ids_json,
            last_material_change_at, next_refresh_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(canonical_claim_key) DO UPDATE SET
            current_status = excluded.current_status,
            active_observation_ids_json = excluded.active_observation_ids_json,
            last_material_change_at = excluded.last_material_change_at,
            next_refresh_at = excluded.next_refresh_at
        """,
        (
            projection.canonical_claim_key,
            projection.current_status.value,
            json.dumps(projection.active_observation_ids),
            _utc_text(projection.last_material_change_at),
            _optional_utc_text(projection.next_refresh_at),
        ),
    )


def _load_observations(
    connection: sqlite3.Connection,
    canonical_claim_key: str,
) -> tuple[ClaimObservation, ...]:
    rows: list[sqlite3.Row] = connection.execute(
        """
        SELECT observation_id, canonical_claim_key, claim_text, claim_kind,
               subjects_json, instruments_json, source_item_id,
               evidence_fragment_ids_json, asserted_at, recorded_at, valid_from,
               horizon, expires_at, supersedes_observation_id
        FROM claim_observations
        WHERE canonical_claim_key = ?
        ORDER BY recorded_at, asserted_at, observation_id
        """,
        (canonical_claim_key,),
    ).fetchall()
    try:
        return tuple(_observation_from_row(row) for row in rows)
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
        raise MalformedClaimRecordError("Stored claim observation is malformed") from error


def _load_verifications(
    connection: sqlite3.Connection,
    observation_ids: tuple[str, ...],
) -> tuple[VerificationResult, ...]:
    if not observation_ids:
        return ()
    placeholders: str = ", ".join("?" for _ in observation_ids)
    query: str = (
        "SELECT verification_id, observation_id, status, supporting_evidence_ids_json, "  # nosec B608
        + "contradicting_evidence_ids_json, checked_at, recorded_at, valid_until, "
        + "verifier_version, limitations_json FROM verification_results WHERE observation_id IN ("
        + placeholders
        + ") ORDER BY recorded_at, checked_at, verification_id"
    )
    rows: list[sqlite3.Row] = connection.execute(query, observation_ids).fetchall()
    try:
        return tuple(_verification_from_row(row) for row in rows)
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
        raise MalformedClaimRecordError("Stored verification result is malformed") from error


def _observation_from_row(row: sqlite3.Row) -> ClaimObservation:
    return ClaimObservation(
        observation_id=str(_column(row, "observation_id")),
        canonical_claim_key=str(_column(row, "canonical_claim_key")),
        claim_text=str(_column(row, "claim_text")),
        claim_kind=ClaimKind(str(_column(row, "claim_kind"))),
        subjects=_json_string_tuple(_column(row, "subjects_json")),
        instruments=_json_string_tuple(_column(row, "instruments_json")),
        source_item_id=str(_column(row, "source_item_id")),
        evidence_fragment_ids=_json_string_tuple(_column(row, "evidence_fragment_ids_json")),
        asserted_at=_datetime_from_database(_column(row, "asserted_at")),
        recorded_at=_datetime_from_database(_column(row, "recorded_at")),
        valid_from=_optional_datetime_from_database(_column(row, "valid_from")),
        horizon=_optional_text(_column(row, "horizon")),
        expires_at=_optional_datetime_from_database(_column(row, "expires_at")),
        supersedes_observation_id=_optional_text(_column(row, "supersedes_observation_id")),
    )


def _verification_from_row(row: sqlite3.Row) -> VerificationResult:
    return VerificationResult(
        verification_id=str(_column(row, "verification_id")),
        observation_id=str(_column(row, "observation_id")),
        status=VerificationStatus(str(_column(row, "status"))),
        supporting_evidence_ids=_json_string_tuple(_column(row, "supporting_evidence_ids_json")),
        contradicting_evidence_ids=_json_string_tuple(_column(row, "contradicting_evidence_ids_json")),
        checked_at=_datetime_from_database(_column(row, "checked_at")),
        recorded_at=_datetime_from_database(_column(row, "recorded_at")),
        valid_until=_optional_datetime_from_database(_column(row, "valid_until")),
        verifier_version=str(_column(row, "verifier_version")),
        limitations=_json_string_tuple(_column(row, "limitations_json")),
    )


def _projection_from_row(row: sqlite3.Row) -> CanonicalClaim:
    try:
        return CanonicalClaim(
            canonical_claim_key=str(_column(row, "canonical_claim_key")),
            current_status=ClaimStatus(str(_column(row, "current_status"))),
            active_observation_ids=_json_string_tuple(_column(row, "active_observation_ids_json")),
            last_material_change_at=_datetime_from_database(_column(row, "last_material_change_at")),
            next_refresh_at=_optional_datetime_from_database(_column(row, "next_refresh_at")),
        )
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
        raise MalformedClaimRecordError("Stored canonical claim is malformed") from error


def _json_string_tuple(value: object) -> tuple[str, ...]:
    parsed: object = cast("object", json.loads(str(value)))
    if not isinstance(parsed, list):
        raise TypeError("Stored JSON value must be an array")
    values: list[str] = []
    for item in cast("list[object]", parsed):
        if not isinstance(item, str):
            raise TypeError("Stored JSON array values must be text")
        values.append(item)
    return tuple(values)


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])


def _datetime_from_database(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Stored datetime must be text")
    parsed: datetime = datetime.fromisoformat(value)
    _require_aware(parsed, field_name="stored datetime")
    return parsed


def _optional_datetime_from_database(value: object) -> datetime | None:
    return None if value is None else _datetime_from_database(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Stored optional text must be text or null")
    return value


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _utc_text(value: datetime) -> str:
    _require_aware(value, field_name="datetime")
    return value.astimezone(timezone.utc).isoformat()


def _optional_utc_text(value: datetime | None) -> str | None:
    return _utc_text(value) if value is not None else None
