"""Module containing append-only claim memory and resolved point-in-time projections."""

from __future__ import annotations

# pyright: reportAny=false
import hashlib
import json
import re
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import cast

from pydantic import BaseModel
from pydantic import ValidationError

from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.projection import project_canonical_claim
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimResolutionDecision
from money_pit.schemas.claims import ClaimResolutionKind
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import UnresolvedObservationCursor
from money_pit.schemas.claims import UnresolvedObservationPage
from money_pit.schemas.claims import VerificationEvidenceAuthority
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


if TYPE_CHECKING:
    import sqlite3


_CLAIM_TOKEN_PATTERN: re.Pattern[str] = re.compile(r"[\w]+", flags=re.UNICODE)
_MAX_RESOLUTION_CANDIDATES = 100
_MAX_UNRESOLVED_PAGE = 100
_MAX_QUERY_TOKENS = 16
ModelT = TypeVar("ModelT", bound=BaseModel)
StoredResolution = tuple[ClaimResolutionDecision, str]


class ClaimRepositoryError(StorageError):
    """Base class for invalid durable claim state."""


class ClaimNotFoundError(ClaimRepositoryError):
    """Raised when a requested claim record does not exist."""


class MalformedClaimRecordError(ClaimRepositoryError):
    """Raised when stored claim JSON violates its typed contract."""


class ImmutableClaimCollisionError(ClaimRepositoryError):
    """Raised when an immutable identifier is reused for different content."""


class ClaimSourceItemNotFoundError(ClaimRepositoryError):
    """Raised when an observation references an unknown source item."""


class ClaimEvidenceFragmentNotFoundError(ClaimRepositoryError):
    """Raised when a claim references an unknown evidence fragment."""


class ClaimEvidenceProvenanceError(ClaimRepositoryError):
    """Raised when claim evidence was not acquired from its asserted source."""


class ClaimVerificationPolicyError(ClaimRepositoryError):
    """Raised when a verification disagrees with durable evidence policy."""


class ClaimRepository:
    """Persist immutable claims and derive resolution-controlled projections."""

    def __init__(self, database: Database, *, refresh_policy: ClaimRefreshPolicy) -> None:
        """Bind claim memory to one initialized database and freshness policy."""
        self._database: Database = database
        self._refresh_policy: ClaimRefreshPolicy = refresh_policy

    def append_observation(self, observation: ClaimObservation) -> None:
        """Append one provenance-checked unresolved observation idempotently."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            append_observation_record(connection, observation)

    def append_resolution(self, decision: ClaimResolutionDecision) -> None:
        """Append a decision whose canonical key is derived outside agent control."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            append_resolution_record(connection, decision)

    def append_verification(self, verification: VerificationResult) -> None:
        """Append one verification rederived from durable evidence policy."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            append_verification_record(connection, verification)

    def observations_as_of(self, *, as_of: datetime) -> tuple[ClaimObservation, ...]:
        """Return observations known and asserted by a point-in-time boundary."""
        boundary = _utc_text(as_of)
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT observation_json FROM claim_observations
                WHERE known_at <= ? AND asserted_at <= ?
                ORDER BY known_at, asserted_at, observation_id
                """,
                (boundary, boundary),
            ).fetchall()
        return _models_from_rows(rows, "observation_json", ClaimObservation)

    def observations_by_ids(self, ids: tuple[str, ...]) -> tuple[ClaimObservation, ...]:
        """Return exact observations in caller-supplied ID order."""
        return self._records_by_ids(
            ids,
            table="claim_observations",
            id_column="observation_id",
            json_column="observation_json",
            model_type=ClaimObservation,
        )

    def resolutions_by_ids(self, ids: tuple[str, ...]) -> tuple[ClaimResolutionDecision, ...]:
        """Return exact accepted resolution statements in caller-supplied order."""
        return self._records_by_ids(
            ids,
            table="claim_resolution_decisions",
            id_column="decision_id",
            json_column="decision_json",
            model_type=ClaimResolutionDecision,
        )

    def verifications_by_ids(self, ids: tuple[str, ...]) -> tuple[VerificationResult, ...]:
        """Return exact verifications in caller-supplied order."""
        return self._records_by_ids(
            ids,
            table="verification_results",
            id_column="verification_id",
            json_column="verification_json",
            model_type=VerificationResult,
        )

    def latest_verifications_for_observations(
        self,
        observation_ids: tuple[str, ...],
        *,
        requested_as_of: datetime,
        same_run_verification_ids: tuple[str, ...] = (),
    ) -> dict[str, VerificationResult]:
        """Return each cited observation's latest baseline or exact same-run verification."""
        if len(set(observation_ids)) != len(observation_ids):
            raise ValueError("observation_ids must be unique")
        if len(set(same_run_verification_ids)) != len(same_run_verification_ids):
            raise ValueError("same_run_verification_ids must be unique")
        _ = self.observations_by_ids(observation_ids)
        boundary = _utc_text(requested_as_of)
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT verification_json FROM verification_results
                WHERE observation_id IN (SELECT value FROM json_each(?))
                  AND known_at <= ? AND checked_at <= ?
                ORDER BY observation_id, known_at, checked_at, verification_id
                """,
                (json.dumps(observation_ids, separators=(",", ":")), boundary, boundary),
            ).fetchall()
        baseline = _models_from_rows(rows, "verification_json", VerificationResult)
        deltas = self.verifications_by_ids(same_run_verification_ids)
        allowed_observation_ids = set(observation_ids)
        if any(item.observation_id not in allowed_observation_ids for item in deltas):
            raise ClaimRepositoryError("same-run verification does not belong to a requested observation")
        latest: dict[str, VerificationResult] = {}
        for verification in (*baseline, *deltas):
            current = latest.get(verification.observation_id)
            if current is None or (
                verification.known_at,
                verification.checked_at,
                verification.verification_id,
            ) > (current.known_at, current.checked_at, current.verification_id):
                latest[verification.observation_id] = verification
        return {
            observation_id: latest[observation_id] for observation_id in observation_ids if observation_id in latest
        }

    def unresolved_observation_page(
        self,
        *,
        requested_as_of: datetime,
        after: UnresolvedObservationCursor | None = None,
        limit: int = 50,
    ) -> UnresolvedObservationPage:
        """Return one keyset-bounded page before any per-subject FTS lookup."""
        if not 1 <= limit <= _MAX_UNRESOLVED_PAGE:
            raise ValueError(f"limit must be between 1 and {_MAX_UNRESOLVED_PAGE}.")
        boundary = _utc_text(requested_as_of)
        after_known_at = None if after is None else _utc_text(after.known_at)
        after_asserted_at = None if after is None else _utc_text(after.asserted_at)
        after_id = None if after is None else after.observation_id
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT observation.observation_json
                FROM claim_observations AS observation
                WHERE observation.known_at <= ? AND observation.asserted_at <= ?
                  AND (
                      ? IS NULL
                      OR (observation.known_at, observation.asserted_at,
                          observation.observation_id) > (?, ?, ?)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM claim_resolution_decisions AS resolution
                      WHERE (resolution.subject_observation_id = observation.observation_id
                             OR resolution.object_observation_id = observation.observation_id)
                        AND resolution.known_at <= ? AND resolution.decided_at <= ?
                  )
                ORDER BY observation.known_at, observation.asserted_at,
                         observation.observation_id
                LIMIT ?
                """,
                (
                    boundary,
                    boundary,
                    after_known_at,
                    after_known_at,
                    after_asserted_at,
                    after_id,
                    boundary,
                    boundary,
                    limit + 1,
                ),
            ).fetchall()
        visible = _models_from_rows(rows, "observation_json", ClaimObservation)
        items = visible[:limit]
        next_cursor = None
        if len(visible) > limit:
            last = items[-1]
            next_cursor = UnresolvedObservationCursor(
                known_at=last.known_at,
                asserted_at=last.asserted_at,
                observation_id=last.observation_id,
            )
        return UnresolvedObservationPage(items=items, next_cursor=next_cursor)

    def resolved_claim_keys(
        self,
        observation_ids: tuple[str, ...],
        *,
        requested_as_of: datetime,
        pending_resolutions: tuple[ClaimResolutionDecision, ...] = (),
    ) -> dict[str, str]:
        """Resolve exact observation IDs over a cutoff plus an ordered pending decision batch."""
        baseline = self.observations_as_of(as_of=requested_as_of)
        exact = self.observations_by_ids(observation_ids)
        by_id = {item.observation_id: item for item in baseline}
        by_id.update({item.observation_id: item for item in exact})
        with self._database.transaction() as connection:
            resolutions = list(_resolutions_through(connection, boundary=requested_as_of))
        memberships = _resolved_memberships(by_id, tuple(resolutions))
        for decision in pending_resolutions:
            subject = by_id.get(decision.subject_observation_id)
            object_observation = (
                None if decision.object_observation_id is None else by_id.get(decision.object_observation_id)
            )
            if subject is None or (
                decision.relation is not ClaimResolutionKind.DISTINCT and object_observation is None
            ):
                raise ClaimNotFoundError(
                    f"Pending resolution references an unknown observation: {decision.decision_id}"
                )
            key = _resolution_key(
                decision=decision,
                subject=subject,
                object_observation=object_observation,
                memberships=memberships,
            )
            resolutions.append((decision, key))
            memberships = _resolved_memberships(by_id, tuple(resolutions))
        return {
            observation_id: memberships[observation_id]
            for observation_id in observation_ids
            if observation_id in memberships
        }

    def resolution_candidates(
        self,
        subject_observation_id: str,
        *,
        requested_as_of: datetime,
        same_run_observation_ids: tuple[str, ...] = (),
        limit: int = 20,
    ) -> tuple[ClaimObservation, ...]:
        """Return bounded, stable FTS candidates visible at the requested cutoff."""
        if not 1 <= limit <= _MAX_RESOLUTION_CANDIDATES:
            raise ValueError(f"limit must be between 1 and {_MAX_RESOLUTION_CANDIDATES}.")
        if len(same_run_observation_ids) > _MAX_RESOLUTION_CANDIDATES:
            raise ValueError(f"same_run_observation_ids cannot exceed {_MAX_RESOLUTION_CANDIDATES}.")
        if len(set(same_run_observation_ids)) != len(same_run_observation_ids):
            raise ValueError("same_run_observation_ids must be unique.")
        subject = self.observations_by_ids((subject_observation_id,))[0]
        raw_tokens: list[str] = cast("list[str]", _CLAIM_TOKEN_PATTERN.findall(subject.claim_text))
        tokens: tuple[str, ...] = tuple(dict.fromkeys(token.casefold() for token in raw_tokens))
        selected_tokens = tuple(token for token in tokens if len(token) > 1)[:_MAX_QUERY_TOKENS]
        if not selected_tokens:
            return ()
        match_query = " OR ".join(f'"{token}"' for token in selected_tokens)
        boundary = _utc_text(requested_as_of)
        delta_ids = tuple(identifier for identifier in same_run_observation_ids if identifier != subject_observation_id)
        delta_placeholders = ",".join("?" for _ in delta_ids)
        visibility_sql = "observation.known_at <= ? AND observation.asserted_at <= ?"
        visibility_values: tuple[object, ...] = (boundary, boundary)
        if delta_ids:
            visibility_sql = f"(({visibility_sql}) OR observation.observation_id IN ({delta_placeholders}))"
            visibility_values = (*visibility_values, *delta_ids)
        query = (
            "SELECT observation.observation_json "  # noqa: S608  # nosec B608 -- dynamic text contains generated placeholders only.
            "FROM claim_observation_search AS search "
            "JOIN claim_observations AS observation USING (observation_id) "
            "WHERE claim_observation_search MATCH ? "
            "AND observation.observation_id != ? "
            f"AND ({visibility_sql}) "
            "ORDER BY bm25(claim_observation_search), observation.known_at, "
            "observation.asserted_at, observation.observation_id LIMIT ?"
        )
        with self._database.transaction() as connection:
            rows = connection.execute(
                query,
                (match_query, subject_observation_id, *visibility_values, limit),
            ).fetchall()
        return _models_from_rows(rows, "observation_json", ClaimObservation)

    def history_as_of(
        self,
        canonical_claim_key: str,
        *,
        as_of: datetime,
    ) -> tuple[tuple[ClaimObservation, ...], tuple[VerificationResult, ...]]:
        """Return resolved observations and verifications for one canonical key."""
        observations = self.observations_as_of(as_of=as_of)
        with self._database.transaction() as connection:
            resolutions = _resolutions_through(connection, boundary=as_of)
            verifications = _verifications_through(connection, boundary=as_of)
        memberships = _resolved_memberships({item.observation_id: item for item in observations}, resolutions)
        selected = tuple(item for item in observations if memberships.get(item.observation_id) == canonical_claim_key)
        if not selected:
            raise ClaimNotFoundError(f"Canonical claim not found: {canonical_claim_key}")
        selected_ids = {item.observation_id for item in selected}
        return selected, tuple(item for item in verifications if item.observation_id in selected_ids)

    def verification_evidence_authority(
        self,
        canonical_claim_key: str,
        fragment_ids: tuple[str, ...],
        *,
        as_of: datetime,
    ) -> tuple[VerificationEvidenceAuthority, ...]:
        """Return eligible exact-acquisition authority for claim support at a cutoff."""
        if len(fragment_ids) != len(set(fragment_ids)):
            raise ValueError("fragment_ids must be unique")
        observations, _verifications = self.history_as_of(canonical_claim_key, as_of=as_of)
        trust_categories = {_verification_trust_category(item) for item in observations}
        if len(trust_categories) != 1:
            raise ClaimVerificationPolicyError(
                "Canonical claim observations do not share one verification trust category"
            )
        trust_category = next(iter(trust_categories))
        boundary = _utc_text(as_of)
        with self._database.transaction() as connection:
            origin_groups = _canonical_claim_origin_groups(
                connection,
                observations,
                boundary=boundary,
            )
            authority = tuple(
                item
                for fragment_id in fragment_ids
                for item in _fragment_verification_authority(
                    connection,
                    fragment_id,
                    origin_groups=origin_groups,
                    trust_category=trust_category,
                    boundary=boundary,
                )
            )
        return tuple(
            sorted(
                authority,
                key=lambda item: (
                    item.fragment_id,
                    item.source_item_id,
                    item.source_definition_hash,
                ),
            )
        )

    def projections_as_of(self, *, as_of: datetime) -> tuple[CanonicalClaim, ...]:
        """Project accepted claim memberships visible at one boundary."""
        observations = self.observations_as_of(as_of=as_of)
        with self._database.transaction() as connection:
            resolutions = _resolutions_through(connection, boundary=as_of)
            verifications = _verifications_through(connection, boundary=as_of)
        return self._project(observations, resolutions, verifications, decision_at=as_of)

    def projections_with_deltas(
        self,
        *,
        requested_as_of: datetime,
        observation_ids: tuple[str, ...],
        resolution_ids: tuple[str, ...],
        verification_ids: tuple[str, ...],
    ) -> tuple[CanonicalClaim, ...]:
        """Project historical baseline unioned with exact attributable same-run records."""
        baseline_observations = self.observations_as_of(as_of=requested_as_of)
        delta_observations = self.observations_by_ids(observation_ids)
        baseline_by_id = {item.observation_id: item for item in baseline_observations}
        baseline_by_id.update({item.observation_id: item for item in delta_observations})
        observations = tuple(
            sorted(baseline_by_id.values(), key=lambda item: (item.known_at, item.asserted_at, item.observation_id))
        )
        with self._database.transaction() as connection:
            baseline_resolutions = _resolutions_through(connection, boundary=requested_as_of)
            baseline_verifications = _verifications_through(connection, boundary=requested_as_of)
            delta_resolutions = _stored_resolutions_by_ids(connection, resolution_ids)
        delta_verifications = self.verifications_by_ids(verification_ids)
        resolutions = _deduplicate_resolutions((*baseline_resolutions, *delta_resolutions))
        verifications = _deduplicate_verifications((*baseline_verifications, *delta_verifications))
        actual_times = [requested_as_of]
        actual_times.extend(item.known_at for item in delta_observations)
        actual_times.extend(item[0].known_at for item in delta_resolutions)
        actual_times.extend(item.known_at for item in delta_verifications)
        return self._project(observations, resolutions, verifications, decision_at=max(actual_times))

    def materialize_projections(self, *, as_of: datetime) -> tuple[CanonicalClaim, ...]:
        """Append the complete projection set for one audit boundary."""
        projections = self.projections_as_of(as_of=as_of)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            for projection in projections:
                encoded = projection.model_dump_json()
                _insert_immutable_json(
                    connection,
                    table="canonical_claims",
                    id_column="canonical_claim_key",
                    identifier=projection.canonical_claim_key,
                    json_column="projection_json",
                    json_value=encoded,
                    insert_sql="""
                        INSERT OR IGNORE INTO canonical_claims (
                            canonical_claim_key, projected_as_of, current_status, projection_json
                        ) VALUES (?, ?, ?, ?)
                    """,
                    insert_values=(
                        projection.canonical_claim_key,
                        _utc_text(projection.projected_as_of),
                        projection.current_status.value,
                        encoded,
                    ),
                    additional_identity=("projected_as_of", _utc_text(projection.projected_as_of)),
                )
        return projections

    def _records_by_ids(
        self,
        ids: tuple[str, ...],
        *,
        table: str,
        id_column: str,
        json_column: str,
        model_type: type[ModelT],
    ) -> tuple[ModelT, ...]:
        if not ids:
            return ()
        placeholders = ",".join("?" for _ in ids)
        with self._database.transaction() as connection:
            # Identifiers are module-owned literals; only values remain caller-controlled.
            query = f"SELECT {id_column}, {json_column} FROM {table} WHERE {id_column} IN ({placeholders})"  # noqa: S608  # nosec B608
            rows = connection.execute(query, ids).fetchall()
        records = _models_from_rows(rows, json_column, model_type)
        by_id = {str(_column(row, id_column)): record for row, record in zip(rows, records, strict=True)}
        missing = tuple(identifier for identifier in ids if identifier not in by_id)
        if missing:
            raise ClaimNotFoundError(f"Claim records not found: {', '.join(missing)}")
        return tuple(by_id[identifier] for identifier in ids)

    def _project(
        self,
        observations: tuple[ClaimObservation, ...],
        resolutions: tuple[StoredResolution, ...],
        verifications: tuple[VerificationResult, ...],
        *,
        decision_at: datetime,
    ) -> tuple[CanonicalClaim, ...]:
        by_id = {item.observation_id: item for item in observations}
        memberships = _resolved_memberships(by_id, resolutions)
        updated_ids = {
            decision.object_observation_id
            for decision, _key in resolutions
            if decision.relation is ClaimResolutionKind.UPDATES
        }
        contradicted_keys = {
            memberships[decision.subject_observation_id]
            for decision, _key in resolutions
            if decision.relation is ClaimResolutionKind.CONTRADICTS and decision.subject_observation_id in memberships
        }
        grouped: dict[str, list[ClaimObservation]] = {}
        for observation_id, key in memberships.items():
            if observation_id in by_id and observation_id not in updated_ids:
                grouped.setdefault(key, []).append(by_id[observation_id])
        verification_by_observation: dict[str, list[VerificationResult]] = {}
        for verification in verifications:
            verification_by_observation.setdefault(verification.observation_id, []).append(verification)
        projections: list[CanonicalClaim] = []
        for key in sorted(grouped):
            key_observations = tuple(
                sorted(grouped[key], key=lambda item: (item.known_at, item.asserted_at, item.observation_id))
            )
            projection = project_canonical_claim(
                key_observations,
                tuple(
                    verification
                    for observation in key_observations
                    for verification in verification_by_observation.get(observation.observation_id, ())
                ),
                canonical_claim_key=key,
                as_of=decision_at,
                refresh_policy=self._refresh_policy,
                resolution_change_times=tuple(
                    decision.known_at for decision, resolved_key in resolutions if resolved_key == key
                ),
            )
            if key in contradicted_keys and projection.current_status is ClaimStatus.ACTIVE:
                projection = projection.model_copy(update={"current_status": ClaimStatus.DISPUTED})
            projections.append(projection)
        return tuple(projections)


def append_observation_record(
    connection: sqlite3.Connection,
    observation: ClaimObservation,
) -> None:
    """Append one validated observation inside an existing write transaction."""
    encoded = observation.model_dump_json()
    _require_observation_provenance(connection, observation)
    _insert_immutable_json(
        connection,
        table="claim_observations",
        id_column="observation_id",
        identifier=observation.observation_id,
        json_column="observation_json",
        json_value=encoded,
        insert_sql="""
            INSERT OR IGNORE INTO claim_observations (
                observation_id, claim_text, source_item_id, asserted_at,
                known_at, effective_from, event_at, review_at, valid_until,
                horizon_class, observation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        insert_values=(
            observation.observation_id,
            observation.claim_text,
            observation.source_item_id,
            _utc_text(observation.asserted_at),
            _utc_text(observation.known_at),
            _optional_utc_text(observation.effective_from),
            _optional_utc_text(observation.event_at),
            _optional_utc_text(observation.review_at),
            _optional_utc_text(observation.valid_until),
            observation.horizon_class.value,
            encoded,
        ),
    )
    search_row = connection.execute(
        "SELECT claim_text FROM claim_observation_search WHERE observation_id = ?",
        (observation.observation_id,),
    ).fetchone()
    if search_row is None:
        _ = connection.execute(
            "INSERT INTO claim_observation_search (observation_id, claim_text) VALUES (?, ?)",
            (observation.observation_id, observation.claim_text),
        )
    elif str(search_row[0]) != observation.claim_text:
        raise ImmutableClaimCollisionError(f"Observation ID {observation.observation_id!r} has different indexed text.")


def append_resolution_record(
    connection: sqlite3.Connection,
    decision: ClaimResolutionDecision,
) -> None:
    """Append one deterministic resolution inside an existing write transaction."""
    observations = _observations_through(connection, boundary=decision.known_at)
    by_id = {item.observation_id: item for item in observations}
    try:
        subject = by_id[decision.subject_observation_id]
    except KeyError as error:
        raise ClaimNotFoundError(f"Resolution observation not found: {error.args[0]}") from error
    object_observation = None if decision.object_observation_id is None else by_id.get(decision.object_observation_id)
    if decision.object_observation_id is not None and object_observation is None:
        raise ClaimNotFoundError(f"Resolution observation not found: {decision.object_observation_id}")
    memberships = _resolved_memberships(
        by_id,
        _resolutions_visible(
            connection,
            known_through=decision.known_at,
            decided_through=decision.decided_at,
        ),
    )
    resolved_key = _resolution_key(
        decision=decision,
        subject=subject,
        object_observation=object_observation,
        memberships=memberships,
    )
    encoded = decision.model_dump_json()
    _insert_immutable_json(
        connection,
        table="claim_resolution_decisions",
        id_column="decision_id",
        identifier=decision.decision_id,
        json_column="decision_json",
        json_value=encoded,
        insert_sql="""
            INSERT OR IGNORE INTO claim_resolution_decisions (
                decision_id, subject_observation_id, object_observation_id,
                relation, resolved_canonical_claim_key, decided_at, known_at,
                decision_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        insert_values=(
            decision.decision_id,
            decision.subject_observation_id,
            decision.object_observation_id,
            decision.relation.value,
            resolved_key,
            _utc_text(decision.decided_at),
            _utc_text(decision.known_at),
            encoded,
        ),
    )


def append_verification_record(
    connection: sqlite3.Connection,
    verification: VerificationResult,
) -> None:
    """Append one policy-derived verification inside an existing write transaction."""
    observation_row = connection.execute(
        "SELECT observation_json FROM claim_observations WHERE observation_id = ?",
        (verification.observation_id,),
    ).fetchone()
    if observation_row is None:
        raise ClaimNotFoundError(f"Claim observation not found: {verification.observation_id}")
    try:
        observation = ClaimObservation.model_validate_json(str(observation_row[0]))
    except (ValueError, ValidationError) as error:
        raise MalformedClaimRecordError("Stored claim observation is malformed.") from error
    _require_verification_policy(connection, observation, verification)
    encoded = verification.model_dump_json()
    _insert_immutable_json(
        connection,
        table="verification_results",
        id_column="verification_id",
        identifier=verification.verification_id,
        json_column="verification_json",
        json_value=encoded,
        insert_sql="""
            INSERT OR IGNORE INTO verification_results (
                verification_id, observation_id, status, checked_at,
                known_at, valid_until, verification_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        insert_values=(
            verification.verification_id,
            verification.observation_id,
            verification.status.value,
            _utc_text(verification.checked_at),
            _utc_text(verification.known_at),
            _optional_utc_text(verification.valid_until),
            encoded,
        ),
    )


def deterministic_claim_key(claim_text: str) -> str:
    """Derive canonical identity from normalized accepted statement text."""
    normalized = " ".join(claim_text.split()).casefold()
    return f"claim:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def _resolution_key(
    *,
    decision: ClaimResolutionDecision,
    subject: ClaimObservation,
    object_observation: ClaimObservation | None,
    memberships: dict[str, str],
) -> str:
    if decision.relation is ClaimResolutionKind.DISTINCT:
        return deterministic_claim_key(subject.claim_text)
    if object_observation is None:
        raise MalformedClaimRecordError(f"Non-distinct resolution has no object observation: {decision.decision_id}")
    return memberships.get(
        object_observation.observation_id,
        deterministic_claim_key(object_observation.claim_text),
    )


def _resolved_memberships(
    observations: dict[str, ClaimObservation],
    resolutions: tuple[StoredResolution, ...],
) -> dict[str, str]:
    memberships: dict[str, str] = {}
    for decision, stored_key in sorted(
        resolutions,
        key=lambda item: (item[0].known_at, item[0].decided_at, item[0].decision_id),
    ):
        subject = observations.get(decision.subject_observation_id)
        object_observation = (
            None if decision.object_observation_id is None else observations.get(decision.object_observation_id)
        )
        if subject is None or (decision.relation is not ClaimResolutionKind.DISTINCT and object_observation is None):
            continue
        derived_key = _resolution_key(
            decision=decision,
            subject=subject,
            object_observation=object_observation,
            memberships=memberships,
        )
        if decision.relation is not ClaimResolutionKind.DISTINCT:
            if object_observation is None:
                raise MalformedClaimRecordError(
                    f"Non-distinct resolution has no object observation: {decision.decision_id}"
                )
            _ = memberships.setdefault(object_observation.observation_id, derived_key)
        if stored_key != derived_key:
            raise MalformedClaimRecordError(
                f"Stored resolution key disagrees with deterministic membership: {decision.decision_id}"
            )
        memberships[subject.observation_id] = derived_key
    return memberships


def _observations_through(connection: sqlite3.Connection, *, boundary: datetime) -> tuple[ClaimObservation, ...]:
    text = _utc_text(boundary)
    rows = connection.execute(
        """
        SELECT observation_json FROM claim_observations
        WHERE known_at <= ? AND asserted_at <= ?
        ORDER BY known_at, asserted_at, observation_id
        """,
        (text, text),
    ).fetchall()
    return _models_from_rows(rows, "observation_json", ClaimObservation)


def _resolutions_through(connection: sqlite3.Connection, *, boundary: datetime) -> tuple[StoredResolution, ...]:
    return _resolutions_visible(connection, known_through=boundary, decided_through=boundary)


def _resolutions_visible(
    connection: sqlite3.Connection,
    *,
    known_through: datetime,
    decided_through: datetime,
) -> tuple[StoredResolution, ...]:
    known_text = _utc_text(known_through)
    decided_text = _utc_text(decided_through)
    rows = connection.execute(
        """
        SELECT decision_json, resolved_canonical_claim_key
        FROM claim_resolution_decisions
        WHERE known_at <= ? AND decided_at <= ?
        ORDER BY known_at, decided_at, decision_id
        """,
        (known_text, decided_text),
    ).fetchall()
    return _stored_resolutions_from_rows(rows)


def _stored_resolutions_by_ids(
    connection: sqlite3.Connection,
    ids: tuple[str, ...],
) -> tuple[StoredResolution, ...]:
    if not ids:
        return ()
    placeholders = ",".join("?" for _ in ids)
    # The placeholder list is generated locally and every value remains bound.
    query = f"SELECT decision_id, decision_json, resolved_canonical_claim_key FROM claim_resolution_decisions WHERE decision_id IN ({placeholders})"  # noqa: S608  # nosec B608
    rows = connection.execute(query, ids).fetchall()
    stored = _stored_resolutions_from_rows(rows)
    by_id = {item[0].decision_id: item for item in stored}
    missing = tuple(identifier for identifier in ids if identifier not in by_id)
    if missing:
        raise ClaimNotFoundError(f"Claim resolutions not found: {', '.join(missing)}")
    return tuple(by_id[identifier] for identifier in ids)


def _stored_resolutions_from_rows(rows: list[sqlite3.Row]) -> tuple[StoredResolution, ...]:
    try:
        return tuple(
            (
                ClaimResolutionDecision.model_validate_json(str(_column(row, "decision_json"))),
                str(_column(row, "resolved_canonical_claim_key")),
            )
            for row in rows
        )
    except (ValueError, ValidationError) as error:
        raise MalformedClaimRecordError("Stored claim resolution is malformed.") from error


def _verifications_through(connection: sqlite3.Connection, *, boundary: datetime) -> tuple[VerificationResult, ...]:
    text = _utc_text(boundary)
    rows = connection.execute(
        """
        SELECT verification_json FROM verification_results
        WHERE known_at <= ? AND checked_at <= ?
        ORDER BY known_at, checked_at, verification_id
        """,
        (text, text),
    ).fetchall()
    return _models_from_rows(rows, "verification_json", VerificationResult)


def _deduplicate_resolutions(values: tuple[StoredResolution, ...]) -> tuple[StoredResolution, ...]:
    by_id = {item[0].decision_id: item for item in values}
    return tuple(sorted(by_id.values(), key=lambda item: (item[0].known_at, item[0].decided_at, item[0].decision_id)))


def _deduplicate_verifications(values: tuple[VerificationResult, ...]) -> tuple[VerificationResult, ...]:
    by_id = {item.verification_id: item for item in values}
    return tuple(sorted(by_id.values(), key=lambda item: (item.known_at, item.checked_at, item.verification_id)))


def _require_observation_provenance(connection: sqlite3.Connection, observation: ClaimObservation) -> None:
    if not _row_exists(connection, "source_items", "source_item_id", observation.source_item_id):
        raise ClaimSourceItemNotFoundError(f"Claim source item not found: {observation.source_item_id}")
    for fragment_id in dict.fromkeys(observation.evidence_fragment_ids):
        row = connection.execute(
            "SELECT asset_id FROM evidence_fragments WHERE fragment_id = ?", (fragment_id,)
        ).fetchone()
        if row is None:
            raise ClaimEvidenceFragmentNotFoundError(f"Claim evidence fragment not found: {fragment_id}")
        acquisition = connection.execute(
            """
            SELECT acquisition_id FROM evidence_asset_acquisitions
            WHERE asset_id = ? AND source_item_id = ? LIMIT 1
            """,
            (row[0], observation.source_item_id),
        ).fetchone()
        if acquisition is None:
            raise ClaimEvidenceProvenanceError(
                f"Claim evidence fragment {fragment_id!r} was not acquired from {observation.source_item_id!r}."
            )
        definition = _source_definition_for_acquisition(
            connection,
            fragment_id=fragment_id,
            source_item_id=observation.source_item_id,
        )
        if AllowedUse.INTERPRETATION not in definition.allowed_uses:
            raise ClaimEvidenceProvenanceError(
                f"Claim source policy does not allow interpretation: {observation.source_item_id!r}."
            )


def _require_verification_policy(
    connection: sqlite3.Connection,
    observation: ClaimObservation,
    verification: VerificationResult,
) -> None:
    supporting_ids = tuple(dict.fromkeys(verification.supporting_evidence_ids))
    contradicting_ids = tuple(dict.fromkeys(verification.contradicting_evidence_ids))
    if set(supporting_ids) & set(contradicting_ids):
        raise ClaimVerificationPolicyError("The same evidence fragment cannot support and contradict a claim.")
    origin_groups = _observation_provenance_groups(connection, observation)
    trust_category = _verification_trust_category(observation)
    supporting = _verification_evidence_authority(
        connection,
        supporting_ids,
        origin_groups=origin_groups,
        trust_category=trust_category,
        boundary=_utc_text(verification.checked_at),
    )
    contradicting = _verification_evidence_authority(
        connection,
        contradicting_ids,
        origin_groups=origin_groups,
        trust_category=trust_category,
        boundary=_utc_text(verification.checked_at),
    )
    supporting_qualified = _meets_verification_standard(supporting)
    contradicting_qualified = _meets_verification_standard(contradicting)
    expected_status = (
        VerificationStatus.MIXED
        if supporting_qualified and contradicting_qualified
        else VerificationStatus.SUPPORTED
        if supporting_qualified
        else VerificationStatus.CONTRADICTED
        if contradicting_qualified
        else VerificationStatus.UNRESOLVED
    )
    groups = tuple(sorted({group for group, _level in supporting}))
    if verification.status is not expected_status:
        raise ClaimVerificationPolicyError(
            f"Verification status must be derived as {expected_status.value!r} from durable evidence."
        )
    if verification.independent_provenance_groups != groups:
        raise ClaimVerificationPolicyError("Verification provenance groups must equal the durable independent groups.")


def _observation_provenance_groups(
    connection: sqlite3.Connection,
    observation: ClaimObservation,
) -> frozenset[str]:
    return frozenset(
        _source_definition_for_acquisition(
            connection,
            fragment_id=fragment_id,
            source_item_id=observation.source_item_id,
        ).provenance_group
        for fragment_id in observation.evidence_fragment_ids
    )


def _verification_evidence_authority(
    connection: sqlite3.Connection,
    fragment_ids: tuple[str, ...],
    *,
    origin_groups: frozenset[str],
    trust_category: TrustCategory,
    boundary: str,
) -> tuple[tuple[str, TrustLevel], ...]:
    by_asset: dict[str, tuple[str, TrustLevel]] = {}
    for fragment_id in fragment_ids:
        authority = _fragment_verification_authority(
            connection,
            fragment_id,
            origin_groups=origin_groups,
            trust_category=trust_category,
            boundary=boundary,
        )
        if not authority:
            raise ClaimVerificationPolicyError(
                f"Verification evidence is unauthorized, untrusted, or originating: {fragment_id}"
            )
        selected = authority[0]
        by_asset[selected.asset_id] = (selected.provenance_group, selected.trust_level)
    return tuple(sorted(by_asset.values(), key=lambda value: (value[0], value[1].value)))


def _canonical_claim_origin_groups(
    connection: sqlite3.Connection,
    observations: tuple[ClaimObservation, ...],
    *,
    boundary: str,
) -> frozenset[str]:
    groups: set[str] = set()
    for observation in observations:
        for fragment_id in observation.evidence_fragment_ids:
            row = connection.execute(
                """
                SELECT definition.provenance_group
                FROM evidence_fragments AS fragment
                JOIN evidence_asset_acquisitions AS acquisition
                  ON acquisition.asset_id = fragment.asset_id
                JOIN source_items AS item
                  ON item.source_item_id = acquisition.source_item_id
                 AND item.content_version = acquisition.content_version
                JOIN source_definition_revisions AS definition
                  ON definition.definition_hash = acquisition.source_definition_hash
                WHERE fragment.fragment_id = ?
                  AND acquisition.source_item_id = ?
                  AND acquisition.retrieved_at <= ?
                  AND item.discovered_at <= ?
                  AND definition.registered_at <= ?
                ORDER BY acquisition.retrieved_at DESC, acquisition.acquisition_id DESC
                LIMIT 1
                """,
                (fragment_id, observation.source_item_id, boundary, boundary, boundary),
            ).fetchone()
            if row is None:
                raise ClaimEvidenceProvenanceError(
                    f"Claim evidence was not durably available at the cutoff: {fragment_id!r}."
                )
            groups.add(str(row[0]))
    return frozenset(groups)


def _fragment_verification_authority(
    connection: sqlite3.Connection,
    fragment_id: str,
    *,
    origin_groups: frozenset[str],
    trust_category: TrustCategory,
    boundary: str,
) -> tuple[VerificationEvidenceAuthority, ...]:
    fragment = connection.execute(
        "SELECT asset_id FROM evidence_fragments WHERE fragment_id = ?",
        (fragment_id,),
    ).fetchone()
    if fragment is None:
        raise ClaimEvidenceFragmentNotFoundError(f"Verification evidence fragment not found: {fragment_id}")
    asset_id = str(fragment[0])
    rows = connection.execute(
        """
        SELECT acquisition.source_item_id, acquisition.source_definition_hash,
               definition.definition_json
        FROM evidence_asset_acquisitions AS acquisition
        JOIN source_items AS item
          ON item.source_item_id = acquisition.source_item_id
         AND item.content_version = acquisition.content_version
        JOIN source_definition_revisions AS definition
          ON definition.definition_hash = acquisition.source_definition_hash
        WHERE acquisition.asset_id = ?
          AND acquisition.retrieved_at <= ?
          AND item.discovered_at <= ?
          AND definition.registered_at <= ?
        ORDER BY definition.provenance_group, definition.definition_hash,
                 acquisition.source_item_id, acquisition.retrieved_at,
                 acquisition.acquisition_id
        """,
        (asset_id, boundary, boundary, boundary),
    ).fetchall()
    eligible: list[VerificationEvidenceAuthority] = []
    for row in rows:
        definition = _source_definition_from_json(str(row[2]))
        level = _trust_level_for(definition, trust_category)
        if (
            AllowedUse.FACTUAL_VERIFICATION in definition.allowed_uses
            and definition.provenance_group not in origin_groups
            and level in {TrustLevel.AUTHORITATIVE_PRIMARY, TrustLevel.INDEPENDENT_SECONDARY}
        ):
            eligible.append(
                VerificationEvidenceAuthority(
                    fragment_id=fragment_id,
                    asset_id=asset_id,
                    source_item_id=str(row[0]),
                    source_definition_hash=str(row[1]),
                    provenance_group=definition.provenance_group,
                    trust_category=trust_category,
                    trust_level=level,
                    allowed_uses=definition.allowed_uses,
                )
            )
    if not eligible:
        return ()
    return (
        min(
            eligible,
            key=lambda item: (
                item.provenance_group,
                item.trust_level.value,
                item.source_definition_hash,
                item.source_item_id,
            ),
        ),
    )


def _meets_verification_standard(authority: tuple[tuple[str, TrustLevel], ...]) -> bool:
    return (
        any(level is TrustLevel.AUTHORITATIVE_PRIMARY for _group, level in authority)
        or len({group for group, _level in authority}) >= 2
    )


def _verification_trust_category(observation: ClaimObservation) -> TrustCategory:
    if observation.category.value == TrustCategory.MARKET.value:
        return TrustCategory.MARKET
    if observation.category.value == TrustCategory.PORTFOLIO.value:
        return TrustCategory.PORTFOLIO
    return TrustCategory.FACTUAL


def _source_definition_for_acquisition(
    connection: sqlite3.Connection,
    *,
    fragment_id: str,
    source_item_id: str,
) -> SourceDefinition:
    row = connection.execute(
        """
        SELECT definition.definition_json
        FROM evidence_fragments AS fragment
        JOIN evidence_asset_acquisitions AS acquisition
          ON acquisition.asset_id = fragment.asset_id
        JOIN source_definition_revisions AS definition
          ON definition.definition_hash = acquisition.source_definition_hash
        WHERE fragment.fragment_id = ? AND acquisition.source_item_id = ?
        ORDER BY acquisition.retrieved_at DESC, acquisition.acquisition_id DESC
        LIMIT 1
        """,
        (fragment_id, source_item_id),
    ).fetchone()
    if row is None:
        raise ClaimEvidenceProvenanceError(
            f"Evidence fragment {fragment_id!r} is not owned by source item {source_item_id!r}."
        )
    return _source_definition_from_json(str(row[0]))


def _source_definition_from_json(value: str) -> SourceDefinition:
    try:
        return SourceDefinition.model_validate_json(value)
    except (ValueError, ValidationError) as error:
        raise MalformedClaimRecordError("Stored source definition is malformed.") from error


def _trust_level_for(definition: SourceDefinition, category: TrustCategory) -> TrustLevel:
    return next(
        (setting.level for setting in definition.trust_settings if setting.category is category),
        TrustLevel.UNTRUSTED,
    )


def _models_from_rows(rows: list[sqlite3.Row], json_column: str, model_type: type[ModelT]) -> tuple[ModelT, ...]:
    try:
        return tuple(model_type.model_validate_json(str(_column(row, json_column))) for row in rows)
    except (ValueError, ValidationError) as error:
        raise MalformedClaimRecordError(f"Stored {json_column} is malformed.") from error


def _insert_immutable_json(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    identifier: str,
    json_column: str,
    json_value: str,
    insert_sql: str,
    insert_values: tuple[object, ...],
    additional_identity: tuple[str, str] | None = None,
) -> None:
    _ = connection.execute(insert_sql, insert_values)
    identity_sql = f"{id_column} = ?"
    identity_values = (identifier,)
    if additional_identity is not None:
        identity_sql += f" AND {additional_identity[0]} = ?"
        identity_values += (additional_identity[1],)
    # Table and column names are selected by internal repository writers.
    query = f"SELECT {json_column} FROM {table} WHERE {identity_sql}"  # noqa: S608  # nosec B608
    row = connection.execute(query, identity_values).fetchone()
    if row is None or str(row[0]) != json_value:
        raise ImmutableClaimCollisionError(f"{table} identifier is bound to different content: {identifier}")


def _row_exists(connection: sqlite3.Connection, table: str, column: str, value: str) -> bool:
    # Table and column names are selected by internal repository writers.
    query = f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1"  # noqa: S608  # nosec B608
    return connection.execute(query, (value,)).fetchone() is not None


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat()


def _optional_utc_text(value: datetime | None) -> str | None:
    return None if value is None else _utc_text(value)
