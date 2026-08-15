"""Module containing durable semantic identities for intelligence work."""

# pyright: reportAny=false

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import cast

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import TypeAdapter
from pydantic import ValidationError

from money_pit.schemas.theses import CandidateThesis
from money_pit.semantic_identity import HYPOTHESIS_REVIEW_POLICY_VERSION
from money_pit.semantic_identity import CandidateSemanticVariant
from money_pit.semantic_identity import CapitalReferenceKind
from money_pit.semantic_identity import candidate_review_dimensions
from money_pit.semantic_identity import candidate_semantic_variant
from money_pit.semantic_identity import canonical_hypothesis_group_id
from money_pit.semantic_identity import research_case_id
from money_pit.semantic_identity import research_task_semantics_from_mapping
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


if TYPE_CHECKING:
    import sqlite3


_FROZEN: ConfigDict = ConfigDict(frozen=True, extra="forbid")
_TASK_PAYLOAD_ADAPTER = TypeAdapter(dict[str, object])
_STRING_LIST_ADAPTER = TypeAdapter(list[str])


class SemanticIntelligenceError(StorageError):
    """Base class for invalid semantic intelligence state."""


class SemanticIdentityError(SemanticIntelligenceError):
    """Raised when a proposal cannot form a strict semantic identity."""


class SemanticTransitionError(SemanticIntelligenceError):
    """Raised when a semantic lifecycle transition is invalid."""


class HypothesisMembershipKind(StrEnum):
    """How a proposal became attached to one canonical hypothesis."""

    EXACT = "exact"


class HypothesisReviewStatus(StrEnum):
    """Lifecycle of a possible semantic-duplicate review."""

    PENDING = "pending"
    SAME = "same"
    DISTINCT = "distinct"


class HypothesisReviewDecision(StrEnum):
    """Operator decisions accepted for a pending duplicate review."""

    SAME = "same"
    DISTINCT = "distinct"


class ResearchTaskRole(StrEnum):
    """Semantic role of a task within one research premise."""

    INITIAL = "initial"
    PLANNER_FOLLOWUP = "planner_followup"


class ResearchTaskExecutionStatus(StrEnum):
    """Job-local execution state of an immutable task definition."""

    PENDING = "pending"
    MATERIALIZED = "materialized"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REUSED = "reused"


class ResearchJobDisposition(StrEnum):
    """Whether a legacy or current research job may own future work."""

    CURRENT = "current"
    SUPERSEDED = "superseded"
    UNAVAILABLE = "unavailable"


class SynthesisEligibility(StrEnum):
    """Deterministic gate before any synthesis inference."""

    ELIGIBLE = "eligible"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNAVAILABLE = "unavailable"


class SynthesisDisposition(StrEnum):
    """Whether a synthesis unit remains the authoritative material-state unit."""

    CURRENT = "current"
    SUPERSEDED = "superseded"
    UNAVAILABLE = "unavailable"


class HypothesisVariant(BaseModel):
    """One immutable exact semantic variant independent of proposal prose."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    variant_id: str = Field(min_length=1)
    semantic_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    capital_kind: CapitalReferenceKind
    capital_reference: str = Field(min_length=1)
    availability: str = Field(pattern=r"^(available|unavailable)$")
    direction: str = Field(pattern=r"^(long|bearish|neutral)$")
    horizon_class: str = Field(pattern=r"^(event|tactical|medium_term|structural)$")
    theme_key: str | None = None
    causal_mechanisms: tuple[str, ...] = ()
    regime_assumptions: tuple[str, ...] = ()
    created_at: AwareDatetime


class CanonicalHypothesisGroup(BaseModel):
    """One current or superseded equivalence group of exact variants."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    group_id: str = Field(min_length=1)
    variant_ids: tuple[str, ...] = Field(min_length=1)
    status: str = Field(pattern=r"^(current|superseded)$")
    created_at: AwareDatetime


class HypothesisMembership(BaseModel):
    """Durable binding from an immutable proposal to a hypothesis."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    candidate_thesis_id: str = Field(min_length=1)
    variant_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    membership_kind: HypothesisMembershipKind
    recorded_at: AwareDatetime

    @property
    def hypothesis_id(self) -> str:
        """Return the effective canonical group identity for compatibility."""
        return self.group_id


class HypothesisReview(BaseModel):
    """Operator review of a coarse but not exact semantic match."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    review_id: str = Field(min_length=1)
    subject_candidate_id: str = Field(min_length=1)
    comparison_candidate_id: str = Field(min_length=1)
    subject_variant_id: str = Field(min_length=1)
    comparison_variant_id: str = Field(min_length=1)
    coarse_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: HypothesisReviewStatus
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    actor: str | None = None
    reason: str | None = None


class CandidateSemanticReconciliation(BaseModel):
    """Result of attaching one proposal and discovering coarse reviews."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    membership: HypothesisMembership
    reviews: tuple[HypothesisReview, ...] = ()


class ResearchJobTaskBinding(BaseModel):
    """One immutable task definition bound to a job-local execution."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    task_id: str = Field(min_length=1)
    role: ResearchTaskRole
    execution_status: ResearchTaskExecutionStatus = ResearchTaskExecutionStatus.PENDING
    origin_unit_ids: tuple[str, ...] = ()
    materialized_session_id: str | None = None
    materialized_task_id: str | None = None
    completed_at: AwareDatetime | None = None
    reused_from_job_id: str | None = None
    reused_from_task_id: str | None = None


class ResearchJobSemanticRecord(BaseModel):
    """Semantic ownership and succession of one existing research job."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    job_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_premise_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_job_ids: tuple[str, ...] = ()
    successor_job_id: str | None = None
    disposition: ResearchJobDisposition
    recorded_at: AwareDatetime


class SynthesisMaterialState(BaseModel):
    """Decision-relevant state used to gate and deduplicate synthesis."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    material_state_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    material_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligibility: SynthesisEligibility
    prior_revision_id: str | None = None
    created_at: AwareDatetime
    assessment: JsonValue
    research_job_ids: tuple[str, ...] = ()


class SemanticWorkStatus(BaseModel):
    """Provider-free counts of semantically unavailable or superseded work."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    source_id: str | None = None
    needs_review: int = Field(ge=0)
    unavailable_research_jobs: int = Field(ge=0)
    superseded_research_jobs: int = Field(ge=0)
    unavailable_synthesis_units: int = Field(ge=0)
    superseded_synthesis_units: int = Field(ge=0)
    insufficient_evidence_assessments: int = Field(ge=0)


class CandidateHypothesisLineage(BaseModel):
    """One proposal membership shown in semantic lineage."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    candidate_thesis_id: str
    variant_id: str
    group_id: str
    membership_kind: HypothesisMembershipKind
    discovery_origin_unit_ids: tuple[str, ...] = ()


class ResearchJobLineage(BaseModel):
    """One research head or predecessor with its job-local executions."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    semantic: ResearchJobSemanticRecord
    tasks: tuple[ResearchJobTaskBinding, ...] = ()


class SynthesisUnitLineage(BaseModel):
    """One material assessment and its synthesis/output disposition."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    material_state: SynthesisMaterialState
    unit_id: str
    disposition: SynthesisDisposition
    successor_unit_id: str | None = None
    successor_research_job_id: str | None = None
    output_record_ids: tuple[str, ...] = ()
    thesis_revision_ids: tuple[str, ...] = ()


class SemanticLineage(BaseModel):
    """Provider-free semantic ancestors and descendants of one durable ID."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    durable_id: str = Field(min_length=1)
    durable_kind: str = Field(min_length=1)
    hypothesis_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    research_job_ids: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    synthesis_unit_ids: tuple[str, ...] = ()
    origin_unit_ids: tuple[str, ...] = ()
    variants: tuple[HypothesisVariant, ...] = ()
    groups: tuple[CanonicalHypothesisGroup, ...] = ()
    memberships: tuple[CandidateHypothesisLineage, ...] = ()
    reviews: tuple[HypothesisReview, ...] = ()
    research_jobs: tuple[ResearchJobLineage, ...] = ()
    synthesis: tuple[SynthesisUnitLineage, ...] = ()


class PortfolioIntelligenceEligibility(BaseModel):
    """Fail-closed semantic and evidence gates for one thesis revision."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    revision_id: str = Field(min_length=1)
    intelligence_available: bool
    synthesis_evidence_sufficient: bool


def hypothesis_variant(candidate: CandidateThesis, *, created_at: datetime) -> HypothesisVariant:
    """Project one proposal through the shared exact semantic identity authority."""
    variant = candidate_semantic_variant(candidate)
    return HypothesisVariant(
        variant_id=variant.variant_id,
        semantic_fingerprint=variant.fingerprint,
        capital_kind=variant.capital_kind,
        capital_reference=variant.capital_reference,
        availability="available" if variant.capital_available else "unavailable",
        direction=variant.direction.value,
        horizon_class=variant.horizon_class.value,
        theme_key=variant.theme,
        causal_mechanisms=variant.causal_mechanisms,
        regime_assumptions=variant.regime_assumptions,
        created_at=created_at,
    )


def coarse_hypothesis_fingerprint(hypothesis: HypothesisVariant) -> str:
    """Return the only automatic review candidate predicate."""
    return _fingerprint(
        {
            "capital_kind": hypothesis.capital_kind.value,
            "capital_reference": hypothesis.capital_reference,
            "direction": hypothesis.direction,
        }
    )


def _initial_group_id(variant_id: str) -> str:
    return canonical_hypothesis_group_id((variant_id,))


def reconcile_unbound_research_sessions(database: Database, source_id: str | None = None) -> int:
    """Bind valid job-scoped sessions without repairing malformed ownership."""
    bound = 0
    with database.transaction(TransactionMode.WRITE) as connection:
        rows = connection.execute(
            """SELECT session.session_id, session.scope_subject_id, session.started_at
            FROM research_sessions session
            LEFT JOIN research_job_sessions binding USING (session_id)
            WHERE session.scope_kind = 'candidate_thesis'
              AND instr(session.scope_subject_id, '|research-job:') > 0
              AND binding.session_id IS NULL
            ORDER BY session.started_at, session.session_id"""
        ).fetchall()
        for row in rows:
            candidate_id, separator, job_id = str(row[1]).partition("|research-job:")
            if not separator or not candidate_id or not job_id:
                continue
            owner = connection.execute(
                """SELECT job.candidate_thesis_id, semantic.disposition, research_case.hypothesis_id
                FROM research_jobs job
                JOIN research_job_semantics semantic USING (job_id)
                JOIN research_cases research_case USING (case_id)
                JOIN canonical_hypothesis_groups hypothesis_group
                  ON hypothesis_group.group_id = research_case.hypothesis_id
                WHERE job.job_id = ? AND hypothesis_group.status = 'current'""",
                (job_id,),
            ).fetchone()
            if owner is None or str(owner[0]) != candidate_id or str(owner[1]) != "current":
                continue
            if not _group_research_available(connection, str(owner[2])):
                continue
            if source_id is not None and not _job_has_exact_source_scope(connection, job_id, source_id):
                continue
            _ = connection.execute(
                """INSERT INTO research_job_sessions (job_id, session_id, bound_at)
                VALUES (?, ?, ?)""",
                (job_id, str(row[0]), str(row[2])),
            )
            bound += 1
    return bound


class SemanticIntelligenceRepository:
    """Own canonical hypotheses, semantic succession, and synthesis gates."""

    _database: Database

    def __init__(self, database: Database) -> None:
        """Bind the semantic repository to one initialized database."""
        self._database = database

    def reconcile_candidate(
        self,
        candidate: CandidateThesis,
        *,
        origin_unit_ids: tuple[str, ...] = (),
        recorded_at: datetime,
    ) -> CandidateSemanticReconciliation:
        """Attach an exact proposal and create only deterministic coarse reviews."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            return _reconcile_candidate(connection, candidate, origin_unit_ids, recorded_at)

    def admit_candidate_semantics(
        self,
        candidate: CandidateThesis,
        *,
        batch_id: str,
        origin_unit_ids: tuple[str, ...],
        recorded_at: datetime,
    ) -> CandidateSemanticReconciliation:
        """Atomically admit a proposal, exact origins, and its semantic identity."""
        if not origin_unit_ids:
            raise SemanticTransitionError("Candidate admission requires at least one discovery origin.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """INSERT INTO candidate_theses
                (candidate_thesis_id, status, created_at, known_at, candidate_json)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(candidate_thesis_id) DO NOTHING""",
                (
                    candidate.candidate_thesis_id,
                    candidate.status.value,
                    _time(candidate.created_at),
                    _time(candidate.known_at),
                    candidate.model_dump_json(),
                ),
            )
            stored = connection.execute(
                "SELECT candidate_json FROM candidate_theses WHERE candidate_thesis_id = ?",
                (candidate.candidate_thesis_id,),
            ).fetchone()
            if stored is None or CandidateThesis.model_validate_json(str(stored[0])) != candidate:
                raise SemanticIdentityError("Candidate identity is bound to different immutable content.")
            batch_units = {
                str(row[0])
                for row in connection.execute(
                    "SELECT unit_id FROM discovery_batch_units WHERE batch_id = ?", (batch_id,)
                ).fetchall()
            }
            requested_origins = set(origin_unit_ids)
            if requested_origins != batch_units:
                raise SemanticTransitionError("Candidate discovery origins must equal the complete discovery batch.")
            return _reconcile_candidate(
                connection,
                candidate,
                origin_unit_ids,
                recorded_at,
                batch_id=batch_id,
            )

    def hypothesis_id_for_candidate(self, candidate_thesis_id: str) -> str | None:
        """Return the effective canonical hypothesis for a proposal."""
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT variant_id FROM candidate_hypothesis_memberships WHERE candidate_thesis_id = ?",
                (candidate_thesis_id,),
            ).fetchone()
            return None if row is None else _effective_group_for_variant(connection, str(row[0]))

    def candidate_ids_for_hypothesis(self, hypothesis_id: str) -> tuple[str, ...]:
        """Return proposals attached to a hypothesis or one of its resolved aliases."""
        with self._database.transaction() as connection:
            effective = _effective_group_id(connection, hypothesis_id)
            rows = connection.execute(
                "SELECT candidate_thesis_id, variant_id FROM candidate_hypothesis_memberships ORDER BY candidate_thesis_id"
            ).fetchall()
            return tuple(
                str(row[0]) for row in rows if _effective_group_for_variant(connection, str(row[1])) == effective
            )

    def research_group_id_for_candidate(self, candidate_thesis_id: str) -> str | None:
        """Return the effective group only when research is semantically available."""
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT variant_id FROM candidate_hypothesis_memberships WHERE candidate_thesis_id = ?",
                (candidate_thesis_id,),
            ).fetchone()
            if row is None:
                return None
            group_id = _effective_group_for_variant(connection, str(row[0]))
            return group_id if _group_research_available(connection, group_id) else None

    def list_reviews(
        self,
        source_id: str | None = None,
        *,
        status: HypothesisReviewStatus | None = None,
    ) -> tuple[HypothesisReview, ...]:
        """Return duplicate reviews without provider or credential access."""
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("COALESCE(resolution.decision, 'pending') = ?")
            params.append(status.value)
        if source_id is not None:
            clauses.append(
                """EXISTS (SELECT 1 FROM candidate_discovery_origins origin
                JOIN discovery_units unit ON unit.unit_id = origin.unit_id
                WHERE origin.candidate_thesis_id IN (review.subject_candidate_id, review.comparison_candidate_id)
                  AND unit.source_id = ?)"""
            )
            params.append(source_id)
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"""SELECT review.*, resolution.decision, resolution.resolved_at,
                    resolution.actor, resolution.reason FROM hypothesis_reviews review
                    LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
                    {where} ORDER BY review.created_at, review.review_id""",  # noqa: S608  # nosec B608 -- fixed internal clause.
                params,
            ).fetchall()
            return tuple(_review(row) for row in rows)

    def resolve_review(
        self,
        review_id: str,
        *,
        decision: HypothesisReviewDecision,
        actor: str,
        reason: str,
        resolved_at: datetime,
    ) -> HypothesisReview:
        """Append one immutable review decision and merge groups only for SAME."""
        if not actor.strip() or not reason.strip():
            raise SemanticTransitionError("Review actor and reason must be non-empty.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            row = connection.execute(
                """SELECT review.*, resolution.decision, resolution.resolved_at,
                resolution.actor, resolution.reason FROM hypothesis_reviews review
                LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
                WHERE review.review_id = ?""",
                (review_id,),
            ).fetchone()
            if row is None:
                raise SemanticTransitionError("Hypothesis review does not exist.")
            existing = _review(row)
            if existing.status is not HypothesisReviewStatus.PENDING:
                if existing.status.value != decision.value:
                    raise SemanticTransitionError("Hypothesis review already has a different immutable decision.")
                return existing
            _ = connection.execute(
                """INSERT INTO hypothesis_review_resolutions
                (review_id, decision, resolved_at, actor, reason, resolution_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    review_id,
                    decision.value,
                    _time(resolved_at),
                    actor.strip(),
                    reason.strip(),
                    _json({"policy_version": HYPOTHESIS_REVIEW_POLICY_VERSION}),
                ),
            )
            if decision is HypothesisReviewDecision.SAME:
                _ = _merge_hypothesis_groups(connection, existing, resolved_at)
            resolved_row = connection.execute(
                """SELECT review.*, resolution.decision, resolution.resolved_at,
                resolution.actor, resolution.reason FROM hypothesis_reviews review
                JOIN hypothesis_review_resolutions resolution USING (review_id)
                WHERE review.review_id = ?""",
                (review_id,),
            ).fetchone()
            if resolved_row is None:
                raise SemanticTransitionError("Resolved hypothesis review disappeared.")
            return _review(resolved_row)

    def ensure_research_job_semantics(
        self,
        *,
        job_id: str,
        hypothesis_id: str,
        scope_fingerprint: str,
        semantic_premise_fingerprint: str,
        task_bindings: tuple[ResearchJobTaskBinding, ...],
        predecessor_job_ids: tuple[str, ...] = (),
        recorded_at: datetime,
    ) -> ResearchJobSemanticRecord:
        """Bind an existing job as the sole current head of one semantic case."""
        effective_hypothesis = self._effective_hypothesis(hypothesis_id)
        case_id = research_case_id(effective_hypothesis, scope_fingerprint)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            if connection.execute("SELECT 1 FROM research_jobs WHERE job_id = ?", (job_id,)).fetchone() is None:
                raise SemanticTransitionError("Research job must exist before semantic binding.")
            _ = connection.execute(
                """INSERT INTO research_cases (case_id, hypothesis_id, scope_fingerprint, head_job_id, created_at)
                VALUES (?, ?, ?, NULL, ?) ON CONFLICT(case_id) DO NOTHING""",
                (case_id, effective_hypothesis, scope_fingerprint, _time(recorded_at)),
            )
            existing_semantic = connection.execute(
                "SELECT * FROM research_job_semantics WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing_semantic is not None:
                return _job_semantic(connection, existing_semantic)
            current = connection.execute(
                "SELECT job_id FROM research_job_semantics WHERE case_id = ? AND disposition = 'current'",
                (case_id,),
            ).fetchone()
            current_id = None if current is None else str(current[0])
            predecessors = tuple(dict.fromkeys((*predecessor_job_ids, *((current_id,) if current_id else ()))))
            if job_id in predecessors:
                raise SemanticTransitionError("Research job cannot succeed itself.")
            inherited_bindings = _successor_task_bindings(connection, predecessors, task_bindings)
            for predecessor in predecessors:
                _supersede_job(connection, predecessor, job_id)
            _ = connection.execute(
                """INSERT INTO research_job_semantics
                (job_id, case_id, semantic_premise_fingerprint,
                 successor_job_id, disposition, recorded_at)
                VALUES (?, ?, ?, NULL, 'current', ?)
                ON CONFLICT(job_id) DO NOTHING""",
                (job_id, case_id, semantic_premise_fingerprint, _time(recorded_at)),
            )
            for predecessor in predecessors:
                _ = connection.execute(
                    "INSERT INTO research_job_predecessors (successor_job_id, predecessor_job_id) VALUES (?, ?)",
                    (job_id, predecessor),
                )
            semantic_row = connection.execute(
                "SELECT * FROM research_job_semantics WHERE job_id = ?", (job_id,)
            ).fetchone()
            if (
                semantic_row is None
                or str(semantic_row[1]) != case_id
                or str(semantic_row[2]) != semantic_premise_fingerprint
            ):
                raise SemanticIdentityError("Research job semantic identity conflicts with durable state.")
            for binding in inherited_bindings:
                _bind_job_task(connection, job_id, binding)
            _ = connection.execute("UPDATE research_cases SET head_job_id = ? WHERE case_id = ?", (job_id, case_id))
            return _job_semantic(connection, semantic_row)

    def supersede_research_job(
        self, *, predecessor_job_id: str, successor_job_id: str, superseded_at: datetime
    ) -> None:
        """Atomically transfer case ownership to an already-bound successor."""
        _ = superseded_at
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _supersede_job(connection, predecessor_job_id, successor_job_id)

    def runnable_research_job_ids(self, source_id: str | None = None) -> tuple[str, ...]:
        """Return current research heads not blocked by duplicate review."""
        params: list[object] = []
        source_clause = ""
        if source_id is not None:
            source_clause = """AND EXISTS (SELECT 1 FROM research_job_discovery_origins origin
            JOIN discovery_units unit ON unit.unit_id = origin.unit_id
            WHERE origin.job_id = semantic.job_id AND unit.source_id = ?)"""
            params.append(source_id)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"""SELECT semantic.job_id FROM research_job_semantics semantic
                JOIN research_cases research_case ON research_case.case_id = semantic.case_id
                JOIN canonical_hypothesis_groups hypothesis_group
                  ON hypothesis_group.group_id = research_case.hypothesis_id
                WHERE semantic.disposition = 'current' AND hypothesis_group.status = 'current' {source_clause}
                  AND NOT EXISTS (
                    SELECT 1 FROM canonical_hypothesis_group_variants group_variant
                    JOIN hypothesis_variants variant USING (variant_id)
                    WHERE group_variant.group_id = research_case.hypothesis_id
                      AND variant.availability = 'unavailable')
                  AND NOT EXISTS (
                    SELECT 1 FROM hypothesis_reviews review
                    LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
                    WHERE resolution.review_id IS NULL AND (
                      review.subject_variant_id IN (
                        SELECT variant_id FROM canonical_hypothesis_group_variants
                        WHERE group_id = research_case.hypothesis_id)
                      OR review.comparison_variant_id IN (
                        SELECT variant_id FROM canonical_hypothesis_group_variants
                        WHERE group_id = research_case.hypothesis_id)))
                ORDER BY semantic.job_id""",  # noqa: S608  # nosec B608 -- fixed internal clause.
                params,
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def current_research_head(
        self,
        *,
        hypothesis_id: str,
        scope_fingerprint: str,
        semantic_premise_fingerprint: str,
    ) -> ResearchJobSemanticRecord | None:
        """Return an exact current head so runtime never recreates migrated semantic work."""
        effective_group_id = self._effective_hypothesis(hypothesis_id)
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT semantic.* FROM research_job_semantics semantic
                JOIN research_cases research_case USING (case_id)
                JOIN canonical_hypothesis_groups hypothesis_group
                  ON hypothesis_group.group_id = research_case.hypothesis_id
                WHERE research_case.hypothesis_id = ? AND research_case.scope_fingerprint = ?
                  AND semantic.semantic_premise_fingerprint = ?
                  AND semantic.disposition = 'current' AND hypothesis_group.status = 'current'""",
                (effective_group_id, scope_fingerprint, semantic_premise_fingerprint),
            ).fetchone()
            return None if row is None else _job_semantic(connection, row)

    def predecessor_research_job_ids(
        self,
        *,
        hypothesis_id: str,
        scope_fingerprint: str,
    ) -> tuple[str, ...]:
        """Return predecessor-group heads due for explicit effective-group reconciliation."""
        effective_group_id = self._effective_hypothesis(hypothesis_id)
        with self._database.transaction() as connection:
            rows = connection.execute(
                """SELECT semantic.job_id, research_case.hypothesis_id
                FROM research_job_semantics semantic
                JOIN research_cases research_case USING (case_id)
                WHERE research_case.scope_fingerprint = ? AND semantic.disposition = 'current'
                ORDER BY semantic.job_id""",
                (scope_fingerprint,),
            ).fetchall()
            return tuple(
                str(row[0])
                for row in rows
                if str(row[1]) != effective_group_id
                and _effective_group_id(connection, str(row[1])) == effective_group_id
            )

    def task_bindings_for_job(
        self,
        job_id: str,
        *,
        due_only: bool = False,
    ) -> tuple[ResearchJobTaskBinding, ...]:
        """Return job-scoped task executions, optionally restricted to pending work."""
        clause = "AND execution_status = 'pending'" if due_only else ""
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"""SELECT * FROM research_job_tasks WHERE job_id = ? {clause}
                ORDER BY role, task_id""",  # noqa: S608  # nosec B608 -- fixed internal clause.
                (job_id,),
            ).fetchall()
            return tuple(_task_binding(connection, row) for row in rows)

    def bind_research_job_tasks(
        self,
        *,
        job_id: str,
        bindings: tuple[ResearchJobTaskBinding, ...],
    ) -> None:
        """Append persisted planner follow-ups to one current research job."""
        if any(binding.role is not ResearchTaskRole.PLANNER_FOLLOWUP for binding in bindings):
            raise SemanticTransitionError("Only planner-follow-up tasks may be appended after case creation.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            current = connection.execute(
                "SELECT 1 FROM research_job_semantics WHERE job_id = ? AND disposition = 'current'",
                (job_id,),
            ).fetchone()
            if current is None:
                raise SemanticTransitionError("Planner tasks require the current semantic research head.")
            for binding in bindings:
                _bind_job_task(connection, job_id, binding)

    def bind_research_session(self, *, job_id: str, session_id: str, bound_at: datetime) -> None:
        """Bind one research session to its exact semantic job owner."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """INSERT INTO research_job_sessions (job_id, session_id, bound_at)
                VALUES (?, ?, ?) ON CONFLICT(session_id) DO NOTHING""",
                (job_id, session_id, _time(bound_at)),
            )
            row = connection.execute(
                "SELECT job_id FROM research_job_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None or str(row[0]) != job_id:
                raise SemanticIdentityError("Research session is already bound to a different job.")

    def session_ids_for_job(self, job_id: str) -> tuple[str, ...]:
        """Return sessions owned by one semantic research job."""
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT session_id FROM research_job_sessions WHERE job_id = ? ORDER BY session_id", (job_id,)
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def materialize_job_task(
        self,
        *,
        job_id: str,
        task_id: str,
        session_id: str,
        materialized_task_id: str,
    ) -> None:
        """Move one pending job-local task into an exact research execution."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """UPDATE research_job_tasks SET execution_status = 'materialized',
                materialized_session_id = ?, materialized_task_id = ?
                WHERE job_id = ? AND task_id = ? AND execution_status = 'pending'""",
                (session_id, materialized_task_id, job_id, task_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    """SELECT execution_status, materialized_session_id, materialized_task_id
                    FROM research_job_tasks WHERE job_id = ? AND task_id = ?""",
                    (job_id, task_id),
                ).fetchone()
                if row is None or tuple(str(value) for value in row) != (
                    ResearchTaskExecutionStatus.MATERIALIZED.value,
                    session_id,
                    materialized_task_id,
                ):
                    raise SemanticTransitionError("Research task cannot be materialized from its current state.")

    def complete_job_task(
        self,
        *,
        job_id: str,
        task_id: str,
        completed_at: datetime,
    ) -> None:
        """Complete one exact materialized job-local task idempotently."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """UPDATE research_job_tasks SET execution_status = 'completed', completed_at = ?
                WHERE job_id = ? AND task_id = ? AND execution_status = 'materialized'""",
                (_time(completed_at), job_id, task_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT execution_status, completed_at FROM research_job_tasks WHERE job_id = ? AND task_id = ?",
                    (job_id, task_id),
                ).fetchone()
                if (
                    row is None
                    or str(row[0]) != ResearchTaskExecutionStatus.COMPLETED
                    or str(row[1]) != _time(completed_at)
                ):
                    raise SemanticTransitionError("Research task cannot be completed from its current state.")

    def release_job_task(self, *, job_id: str, task_id: str) -> None:
        """Release a materialized task whose provider work was not made durable."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """UPDATE research_job_tasks SET execution_status = 'pending',
                materialized_session_id = NULL, materialized_task_id = NULL
                WHERE job_id = ? AND task_id = ? AND execution_status = 'materialized'""",
                (job_id, task_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT execution_status FROM research_job_tasks WHERE job_id = ? AND task_id = ?",
                    (job_id, task_id),
                ).fetchone()
                if row is None or str(row[0]) != ResearchTaskExecutionStatus.PENDING:
                    raise SemanticTransitionError("Research task cannot be released from its current state.")

    def cancel_job_task(self, *, job_id: str, task_id: str, completed_at: datetime) -> None:
        """Cancel one pending job-local obligation with an explicit terminal time."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """UPDATE research_job_tasks SET execution_status = 'cancelled', completed_at = ?
                WHERE job_id = ? AND task_id = ? AND execution_status = 'pending'""",
                (_time(completed_at), job_id, task_id),
            )
            if cursor.rowcount != 1:
                raise SemanticTransitionError("Only a pending research task may be cancelled.")

    def reuse_job_task(
        self,
        *,
        job_id: str,
        task_id: str,
        source_job_id: str,
        source_task_id: str,
        completed_at: datetime,
    ) -> None:
        """Credit one pending obligation from an exact completed predecessor execution."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            source = connection.execute(
                """SELECT completed_at FROM research_job_tasks WHERE job_id = ? AND task_id = ?
                AND execution_status IN ('completed', 'reused')""",
                (source_job_id, source_task_id),
            ).fetchone()
            if source is None or str(source[0]) != _time(completed_at):
                raise SemanticTransitionError("Research task reuse requires an exact completed source execution.")
            cursor = connection.execute(
                """UPDATE research_job_tasks SET execution_status = 'reused', completed_at = ?,
                reused_from_job_id = ?, reused_from_task_id = ?
                WHERE job_id = ? AND task_id = ? AND execution_status = 'pending'""",
                (_time(completed_at), source_job_id, source_task_id, job_id, task_id),
            )
            if cursor.rowcount != 1:
                raise SemanticTransitionError("Only a pending research task may reuse completed work.")

    def ensure_synthesis_material_state(self, state: SynthesisMaterialState) -> SynthesisMaterialState:
        """Persist one decision-relevant synthesis state and its operational origins."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            effective = _effective_group_id(connection, state.hypothesis_id)
            _ = connection.execute(
                """INSERT INTO synthesis_material_states
                (material_state_id, hypothesis_id, material_fingerprint, eligibility,
                 prior_revision_id, created_at, assessment_json)
                VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(material_state_id) DO NOTHING""",
                (
                    state.material_state_id,
                    effective,
                    state.material_fingerprint,
                    state.eligibility.value,
                    state.prior_revision_id,
                    _time(state.created_at),
                    _json(state.assessment),
                ),
            )
            for job_id in state.research_job_ids:
                _ = connection.execute(
                    "INSERT OR IGNORE INTO synthesis_material_origins (material_state_id, research_job_id) VALUES (?, ?)",
                    (state.material_state_id, job_id),
                )
            row = connection.execute(
                "SELECT * FROM synthesis_material_states WHERE material_state_id = ?", (state.material_state_id,)
            ).fetchone()
            if row is None or (
                str(row[1]),
                str(row[2]),
                str(row[3]),
                None if row[4] is None else str(row[4]),
                str(row[6]),
            ) != (
                effective,
                state.material_fingerprint,
                state.eligibility.value,
                state.prior_revision_id,
                _json(state.assessment),
            ):
                raise SemanticIdentityError("Synthesis material identity conflicts with durable state.")
            return _material_state(connection, row)

    def bind_synthesis_unit(
        self,
        *,
        unit_id: str,
        material_state_id: str,
        disposition: SynthesisDisposition,
        recorded_at: datetime,
    ) -> None:
        """Bind an existing A4 unit to its decision-relevant material state."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            if connection.execute("SELECT 1 FROM synthesis_units WHERE unit_id = ?", (unit_id,)).fetchone() is None:
                raise SemanticTransitionError("Synthesis unit must exist before semantic binding.")
            if disposition is SynthesisDisposition.CURRENT:
                existing = connection.execute(
                    """SELECT unit_id FROM synthesis_unit_semantics
                    WHERE material_state_id = ? AND disposition = 'current'""",
                    (material_state_id,),
                ).fetchone()
                if existing is not None and str(existing[0]) != unit_id:
                    raise SemanticIdentityError("Synthesis material already has a different current unit identity.")
            _ = connection.execute(
                """INSERT INTO synthesis_unit_semantics
                (unit_id, material_state_id, disposition, successor_unit_id, recorded_at)
                VALUES (?, ?, ?, NULL, ?) ON CONFLICT(unit_id) DO NOTHING""",
                (unit_id, material_state_id, disposition.value, _time(recorded_at)),
            )
            stored = connection.execute(
                """SELECT material_state_id, disposition FROM synthesis_unit_semantics
                WHERE unit_id = ?""",
                (unit_id,),
            ).fetchone()
            if stored is None or (str(stored[0]), str(stored[1])) != (
                material_state_id,
                disposition.value,
            ):
                raise SemanticIdentityError("Synthesis unit semantic binding conflicts with durable state.")

    def eligible_synthesis_unit_ids(self, source_id: str | None = None) -> tuple[str, ...]:
        """Return runnable A4 units whose material state passes the evidence gate."""
        params: list[object] = []
        source_clause = ""
        if source_id is not None:
            source_clause = """AND EXISTS (SELECT 1 FROM synthesis_material_origins origin
            JOIN research_job_discovery_origins discovery_origin ON discovery_origin.job_id = origin.research_job_id
            JOIN discovery_units unit ON unit.unit_id = discovery_origin.unit_id
            WHERE origin.material_state_id = semantic.material_state_id AND unit.source_id = ?)"""
            params.append(source_id)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"""SELECT semantic.unit_id FROM synthesis_unit_semantics semantic
                JOIN synthesis_material_states material USING (material_state_id)
                JOIN canonical_hypothesis_groups hypothesis_group
                  ON hypothesis_group.group_id = material.hypothesis_id
                WHERE semantic.disposition = 'current' AND material.eligibility = 'eligible'
                  AND hypothesis_group.status = 'current'
                  AND NOT EXISTS (
                    SELECT 1 FROM hypothesis_reviews review
                    LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
                    WHERE resolution.review_id IS NULL AND (
                      review.subject_variant_id IN (
                        SELECT variant_id FROM canonical_hypothesis_group_variants
                        WHERE group_id = material.hypothesis_id)
                      OR review.comparison_variant_id IN (
                        SELECT variant_id FROM canonical_hypothesis_group_variants
                        WHERE group_id = material.hypothesis_id)))
                  {source_clause} ORDER BY semantic.unit_id""",  # noqa: S608  # nosec B608 -- fixed internal clause.
                params,
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def synthesis_unit_id_for_material_state(self, material_state_id: str) -> str | None:
        """Return the authoritative synthesis unit already bound to a material state."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT unit_id FROM synthesis_unit_semantics
                WHERE material_state_id = ? AND disposition = 'current'""",
                (material_state_id,),
            ).fetchone()
            return None if row is None else str(row[0])

    def semantic_status(self, source_id: str | None = None) -> SemanticWorkStatus:
        """Return semantic queue exclusions without constructing providers."""
        with self._database.transaction() as connection:
            source_candidates = _source_candidate_ids(connection, source_id)
            reviews = connection.execute(
                """SELECT review.subject_candidate_id, review.comparison_candidate_id
                FROM hypothesis_reviews review
                LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
                WHERE resolution.review_id IS NULL"""
            ).fetchall()
            needs_review = sum(
                1
                for row in reviews
                if source_id is None or str(row[0]) in source_candidates or str(row[1]) in source_candidates
            )
            research_counts = _semantic_disposition_counts(connection, "research_job_semantics", source_id)
            synthesis_counts = _semantic_disposition_counts(connection, "synthesis_unit_semantics", source_id)
            if source_id is None:
                insufficient_row = connection.execute(
                    """SELECT count(*) FROM synthesis_material_states
                    WHERE eligibility = 'insufficient_evidence'"""
                ).fetchone()
            else:
                insufficient_row = connection.execute(
                    """SELECT count(DISTINCT material.material_state_id)
                    FROM synthesis_material_states material
                    JOIN synthesis_material_origins origin USING (material_state_id)
                    JOIN research_job_discovery_origins discovery_origin
                      ON discovery_origin.job_id = origin.research_job_id
                    JOIN discovery_units unit ON unit.unit_id = discovery_origin.unit_id
                    WHERE material.eligibility = 'insufficient_evidence' AND unit.source_id = ?
                      AND NOT EXISTS (
                        SELECT 1 FROM synthesis_material_origins other_material_origin
                        JOIN research_job_discovery_origins other_discovery_origin
                          ON other_discovery_origin.job_id = other_material_origin.research_job_id
                        JOIN discovery_units other_unit ON other_unit.unit_id = other_discovery_origin.unit_id
                        WHERE other_material_origin.material_state_id = material.material_state_id
                          AND (other_unit.source_id IS NULL OR other_unit.source_id != ?))""",
                    (source_id, source_id),
                ).fetchone()
            return SemanticWorkStatus(
                source_id=source_id,
                needs_review=needs_review,
                unavailable_research_jobs=research_counts.get("unavailable", 0),
                superseded_research_jobs=research_counts.get("superseded", 0),
                unavailable_synthesis_units=synthesis_counts.get("unavailable", 0),
                superseded_synthesis_units=synthesis_counts.get("superseded", 0),
                insufficient_evidence_assessments=int(insufficient_row[0]),
            )

    def lineage(self, durable_id: str) -> SemanticLineage:
        """Return semantic lineage for one namespace-qualified durable identifier."""
        with self._database.transaction() as connection:
            return _lineage(connection, durable_id)

    def portfolio_eligibility_for_revision(self, revision_id: str) -> PortfolioIntelligenceEligibility:
        """Return fail-closed A5 eligibility from durable semantic lineage."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT membership.variant_id
                FROM thesis_revisions revision
                JOIN theses thesis ON thesis.thesis_id = revision.thesis_id
                JOIN candidate_hypothesis_memberships membership
                  ON membership.candidate_thesis_id = thesis.candidate_thesis_id
                WHERE revision.revision_id = ?""",
                (revision_id,),
            ).fetchone()
            if row is None:
                return PortfolioIntelligenceEligibility(
                    revision_id=revision_id,
                    intelligence_available=False,
                    synthesis_evidence_sufficient=False,
                )
            hypothesis_id = _effective_group_for_variant(connection, str(row[0]))
            available = _group_research_available(connection, hypothesis_id)
            sufficient = any(
                _effective_group_id(connection, str(item[0])) == hypothesis_id
                for item in connection.execute(
                    """SELECT material.hypothesis_id FROM synthesis_outputs output
                    JOIN synthesis_unit_semantics semantic ON semantic.unit_id = output.unit_id
                    JOIN synthesis_units unit ON unit.unit_id = semantic.unit_id
                    JOIN synthesis_material_states material USING (material_state_id)
                    WHERE material.eligibility = 'eligible'
                      AND semantic.disposition = 'current'
                      AND unit.status = 'completed'
                      AND EXISTS (SELECT 1 FROM json_each(output.output_record_ids_json) identifier
                                  WHERE identifier.value = ?)""",
                    (f"thesis_revision:{revision_id}",),
                ).fetchall()
            )
            return PortfolioIntelligenceEligibility(
                revision_id=revision_id,
                intelligence_available=available,
                synthesis_evidence_sufficient=available and sufficient,
            )

    def _effective_hypothesis(self, hypothesis_id: str) -> str:
        with self._database.transaction() as connection:
            return _effective_group_id(connection, hypothesis_id)


def reconcile_unbound_candidates(database: Database, source_id: str | None = None) -> tuple[str, ...]:
    """Repair valid origin-grounded A2 proposals without providers or review authority."""
    params: tuple[object, ...] = () if source_id is None else (source_id,)
    source_clause = (
        ""
        if source_id is None
        else """AND EXISTS (
      SELECT 1 FROM candidate_discovery_origins scoped_origin
      JOIN discovery_units scoped_unit USING (unit_id)
      WHERE scoped_origin.candidate_thesis_id = candidate.candidate_thesis_id
        AND scoped_unit.source_id = ?)"""
    )
    reconciled: list[str] = []
    with database.transaction(TransactionMode.WRITE) as connection:
        rows = connection.execute(
            f"""SELECT candidate.candidate_thesis_id, candidate.candidate_json
            FROM candidate_theses candidate
            LEFT JOIN candidate_hypothesis_memberships membership USING (candidate_thesis_id)
            WHERE membership.candidate_thesis_id IS NULL
              AND EXISTS (SELECT 1 FROM candidate_discovery_origins origin
                          WHERE origin.candidate_thesis_id = candidate.candidate_thesis_id)
              {source_clause} ORDER BY candidate.candidate_thesis_id""",  # noqa: S608  # nosec B608 -- fixed internal clause.
            params,
        ).fetchall()
        for row in rows:
            try:
                candidate = CandidateThesis.model_validate_json(str(row[1]))
            except (ValueError, ValidationError):
                continue
            if candidate.candidate_thesis_id != str(row[0]):
                continue
            origin_rows = connection.execute(
                """SELECT origin.unit_id, origin.batch_id, batch_unit.unit_id
                FROM candidate_discovery_origins origin
                LEFT JOIN discovery_batch_units batch_unit
                  ON batch_unit.batch_id = origin.batch_id AND batch_unit.unit_id = origin.unit_id
                WHERE origin.candidate_thesis_id = ? ORDER BY origin.unit_id""",
                (candidate.candidate_thesis_id,),
            ).fetchall()
            if not origin_rows or any(origin[2] is None for origin in origin_rows):
                continue
            _ = _reconcile_candidate(connection, candidate, (), candidate.known_at)
            reconciled.append(candidate.candidate_thesis_id)
    return tuple(reconciled)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SemanticTransitionError("Semantic transition timestamps must be timezone-aware.")
    return value.isoformat()


def _reconcile_candidate(
    connection: sqlite3.Connection,
    candidate: CandidateThesis,
    origin_unit_ids: tuple[str, ...],
    recorded_at: datetime,
    *,
    batch_id: str | None = None,
) -> CandidateSemanticReconciliation:
    db = connection
    variant = hypothesis_variant(candidate, created_at=recorded_at)
    group_id = _initial_group_id(variant.variant_id)
    _insert_variant_and_initial_group(db, variant, group_id)
    _ = db.execute(
        """INSERT INTO candidate_hypothesis_memberships
        (candidate_thesis_id, variant_id, membership_kind, recorded_at)
        VALUES (?, ?, 'exact', ?) ON CONFLICT(candidate_thesis_id) DO NOTHING""",
        (candidate.candidate_thesis_id, variant.variant_id, _time(recorded_at)),
    )
    row = db.execute(
        """SELECT variant_id, membership_kind, recorded_at
        FROM candidate_hypothesis_memberships WHERE candidate_thesis_id = ?""",
        (candidate.candidate_thesis_id,),
    ).fetchone()
    if row is None or str(row[0]) != variant.variant_id:
        raise SemanticIdentityError("Candidate proposal is already bound to a different semantic identity.")
    for unit_id in sorted(set(origin_unit_ids)):
        if batch_id is None:
            raise SemanticTransitionError("Discovery origins require their exact discovery batch identity.")
        _ = db.execute(
            """INSERT INTO candidate_discovery_origins
            (candidate_thesis_id, unit_id, batch_id) VALUES (?, ?, ?)
            ON CONFLICT(candidate_thesis_id, unit_id) DO NOTHING""",
            (candidate.candidate_thesis_id, unit_id, batch_id),
        )
        stored_origin = db.execute(
            """SELECT batch_id FROM candidate_discovery_origins
            WHERE candidate_thesis_id = ? AND unit_id = ?""",
            (candidate.candidate_thesis_id, unit_id),
        ).fetchone()
        if stored_origin is None or str(stored_origin[0]) != batch_id:
            raise SemanticIdentityError("Candidate discovery origin is bound to a different batch.")
    membership = HypothesisMembership(
        candidate_thesis_id=candidate.candidate_thesis_id,
        variant_id=str(row[0]),
        group_id=_effective_group_for_variant(db, str(row[0])),
        membership_kind=HypothesisMembershipKind(str(row[1])),
        recorded_at=datetime.fromisoformat(str(row[2])),
    )
    reviews = _ensure_coarse_reviews(db, candidate.candidate_thesis_id, variant, recorded_at)
    return CandidateSemanticReconciliation(membership=membership, reviews=reviews)


def _insert_variant_and_initial_group(
    connection: sqlite3.Connection,
    variant: HypothesisVariant,
    group_id: str,
) -> None:
    db = connection
    _ = db.execute(
        """INSERT INTO hypothesis_variants
        (variant_id, semantic_fingerprint, capital_kind, capital_reference, availability,
         direction, horizon_class, theme_key, causal_mechanisms_json,
         regime_assumptions_json, created_at, variant_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(variant_id) DO NOTHING""",
        (
            variant.variant_id,
            variant.semantic_fingerprint,
            variant.capital_kind.value,
            variant.capital_reference,
            variant.availability,
            variant.direction,
            variant.horizon_class,
            variant.theme_key,
            _json(variant.causal_mechanisms),
            _json(variant.regime_assumptions),
            _time(variant.created_at),
            _json(variant.model_dump(mode="json")),
        ),
    )
    _ = db.execute(
        """INSERT INTO canonical_hypothesis_groups (group_id, status, created_at)
        VALUES (?, 'current', ?) ON CONFLICT(group_id) DO NOTHING""",
        (group_id, _time(variant.created_at)),
    )
    _ = db.execute(
        """INSERT OR IGNORE INTO canonical_hypothesis_group_variants (group_id, variant_id)
        VALUES (?, ?)""",
        (group_id, variant.variant_id),
    )


def _variant_semantics(variant: HypothesisVariant) -> CandidateSemanticVariant:
    return CandidateSemanticVariant.model_validate(
        {
            "capital_kind": variant.capital_kind,
            "capital_reference": variant.capital_reference,
            "direction": variant.direction,
            "horizon_class": variant.horizon_class,
            "theme": variant.theme_key,
            "causal_mechanisms": variant.causal_mechanisms,
            "regime_assumptions": variant.regime_assumptions,
        }
    )


def _ensure_coarse_reviews(
    connection: sqlite3.Connection,
    candidate_id: str,
    variant: HypothesisVariant,
    at: datetime,
) -> tuple[HypothesisReview, ...]:
    db = connection
    rows = db.execute(
        """SELECT membership.candidate_thesis_id, stored.*
        FROM candidate_hypothesis_memberships membership
        JOIN hypothesis_variants stored USING (variant_id)
        WHERE stored.capital_kind = ? AND stored.capital_reference = ?
          AND stored.direction = ? AND stored.variant_id != ?
        ORDER BY membership.candidate_thesis_id""",
        (variant.capital_kind.value, variant.capital_reference, variant.direction, variant.variant_id),
    ).fetchall()
    reviews: list[HypothesisReview] = []
    for row in rows:
        stored = _variant(row, offset=1)
        differences = candidate_review_dimensions(_variant_semantics(variant), _variant_semantics(stored))
        if not differences:
            continue
        subject_candidate, comparison_candidate = sorted((candidate_id, str(row[0])))
        subject_variant = variant.variant_id if subject_candidate == candidate_id else stored.variant_id
        comparison_variant = stored.variant_id if subject_candidate == candidate_id else variant.variant_id
        coarse = _fingerprint(
            {
                "capital_kind": variant.capital_kind.value,
                "capital_reference": variant.capital_reference,
                "direction": variant.direction,
            }
        )
        review_id = "hypothesis-review:" + _fingerprint(
            (subject_candidate, comparison_candidate, coarse, HYPOTHESIS_REVIEW_POLICY_VERSION)
        )
        _ = db.execute(
            """INSERT INTO hypothesis_reviews
            (review_id, subject_candidate_id, comparison_candidate_id, subject_variant_id,
             comparison_variant_id, coarse_fingerprint, policy_version, created_at, review_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_candidate_id, comparison_candidate_id, policy_version) DO NOTHING""",
            (
                review_id,
                subject_candidate,
                comparison_candidate,
                subject_variant,
                comparison_variant,
                coarse,
                HYPOTHESIS_REVIEW_POLICY_VERSION,
                _time(at),
                _json({"differing_dimensions": differences}),
            ),
        )
        review_row = db.execute(
            """SELECT review.*, resolution.decision, resolution.resolved_at,
            resolution.actor, resolution.reason FROM hypothesis_reviews review
            LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
            WHERE review.review_id = ?""",
            (review_id,),
        ).fetchone()
        if review_row is not None:
            reviews.append(_review(review_row))
    return tuple(reviews)


def _review(row: sqlite3.Row) -> HypothesisReview:
    values = row
    decision = values["decision"]
    return HypothesisReview(
        review_id=str(values["review_id"]),
        subject_candidate_id=str(values["subject_candidate_id"]),
        comparison_candidate_id=str(values["comparison_candidate_id"]),
        subject_variant_id=str(values["subject_variant_id"]),
        comparison_variant_id=str(values["comparison_variant_id"]),
        coarse_fingerprint=str(values["coarse_fingerprint"]),
        status=HypothesisReviewStatus.PENDING if decision is None else HypothesisReviewStatus(str(decision)),
        created_at=datetime.fromisoformat(str(values["created_at"])),
        resolved_at=None if values["resolved_at"] is None else datetime.fromisoformat(str(values["resolved_at"])),
        actor=None if values["actor"] is None else str(values["actor"]),
        reason=None if values["reason"] is None else str(values["reason"]),
    )


def _effective_group_id(connection: sqlite3.Connection, group_id: str) -> str:
    db = connection
    current = group_id
    seen: set[str] = set()
    while True:
        if current in seen:
            raise SemanticIdentityError("Canonical hypothesis group supersession contains a cycle.")
        seen.add(current)
        rows = db.execute(
            """SELECT successor_group_id FROM canonical_hypothesis_group_supersessions
            WHERE predecessor_group_id = ?""",
            (current,),
        ).fetchall()
        if not rows:
            return current
        if len(rows) != 1:
            raise SemanticIdentityError("Canonical hypothesis group has multiple successors.")
        current = str(rows[0][0])


def _effective_group_for_variant(connection: sqlite3.Connection, variant_id: str) -> str:
    db = connection
    roots = {
        _effective_group_id(db, str(row[0]))
        for row in db.execute(
            """SELECT group_id FROM canonical_hypothesis_group_variants
            WHERE variant_id = ? ORDER BY group_id""",
            (variant_id,),
        ).fetchall()
    }
    if len(roots) != 1:
        raise SemanticIdentityError("Hypothesis variant does not resolve to exactly one current group.")
    return roots.pop()


def _group_research_available(connection: sqlite3.Connection, group_id: str) -> bool:
    """Return whether an effective group may create or claim new intelligence work."""
    db = connection
    effective_group_id = _effective_group_id(db, group_id)
    unavailable = db.execute(
        """SELECT 1 FROM canonical_hypothesis_group_variants group_variant
        JOIN hypothesis_variants variant USING (variant_id)
        WHERE group_variant.group_id = ? AND variant.availability = 'unavailable'
        LIMIT 1""",
        (effective_group_id,),
    ).fetchone()
    if unavailable is not None:
        return False
    unresolved_review = db.execute(
        """SELECT 1 FROM hypothesis_reviews review
        LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
        WHERE resolution.review_id IS NULL AND (
            review.subject_variant_id IN (
                SELECT variant_id FROM canonical_hypothesis_group_variants WHERE group_id = ?)
            OR review.comparison_variant_id IN (
                SELECT variant_id FROM canonical_hypothesis_group_variants WHERE group_id = ?))
        LIMIT 1""",
        (effective_group_id, effective_group_id),
    ).fetchone()
    return unresolved_review is None


def _job_has_exact_source_scope(connection: sqlite3.Connection, job_id: str, source_id: str) -> bool:
    rows = connection.execute(
        """SELECT DISTINCT discovery.source_id FROM research_job_discovery_origins origin
        JOIN discovery_units discovery USING (unit_id)
        WHERE origin.job_id = ? ORDER BY discovery.source_id""",
        (job_id,),
    ).fetchall()
    return bool(rows) and all(row[0] is not None and str(row[0]) == source_id for row in rows)


def _merge_hypothesis_groups(connection: sqlite3.Connection, review: HypothesisReview, recorded_at: datetime) -> str:
    db = connection
    subject_variant = _variant_by_id(db, review.subject_variant_id)
    comparison_variant = _variant_by_id(db, review.comparison_variant_id)
    if (
        subject_variant.capital_kind,
        subject_variant.capital_reference,
        subject_variant.direction,
        subject_variant.horizon_class,
    ) != (
        comparison_variant.capital_kind,
        comparison_variant.capital_reference,
        comparison_variant.direction,
        comparison_variant.horizon_class,
    ):
        raise SemanticTransitionError("Same resolution requires equal capital reference, direction, and horizon.")
    left = _effective_group_for_variant(db, review.subject_variant_id)
    right = _effective_group_for_variant(db, review.comparison_variant_id)
    if left == right:
        return left
    left_variants = _group_variant_ids(db, left)
    right_variants = _group_variant_ids(db, right)
    for row in db.execute(
        """SELECT review.subject_variant_id, review.comparison_variant_id
        FROM hypothesis_reviews review JOIN hypothesis_review_resolutions resolution USING (review_id)
        WHERE resolution.decision = 'distinct'"""
    ).fetchall():
        if (str(row[0]) in left_variants and str(row[1]) in right_variants) or (
            str(row[1]) in left_variants and str(row[0]) in right_variants
        ):
            raise SemanticTransitionError("Same resolution conflicts with an immutable distinct decision.")
    successor = canonical_hypothesis_group_id(tuple(sorted((*left_variants, *right_variants))))
    _ = db.execute(
        "INSERT INTO canonical_hypothesis_groups (group_id, status, created_at) VALUES (?, 'current', ?)",
        (successor, _time(recorded_at)),
    )
    for variant_id in sorted((*left_variants, *right_variants)):
        _ = db.execute(
            "INSERT INTO canonical_hypothesis_group_variants (group_id, variant_id) VALUES (?, ?)",
            (successor, variant_id),
        )
    for predecessor in (left, right):
        _ = db.execute(
            """INSERT INTO canonical_hypothesis_group_supersessions
            (successor_group_id, predecessor_group_id, review_id) VALUES (?, ?, ?)""",
            (successor, predecessor, review.review_id),
        )
        _ = db.execute(
            "UPDATE canonical_hypothesis_groups SET status = 'superseded' WHERE group_id = ?",
            (predecessor,),
        )
    return successor


def _group_variant_ids(connection: sqlite3.Connection, group_id: str) -> tuple[str, ...]:
    db = connection
    return tuple(
        str(row[0])
        for row in db.execute(
            """SELECT variant_id FROM canonical_hypothesis_group_variants
            WHERE group_id = ? ORDER BY variant_id""",
            (group_id,),
        ).fetchall()
    )


def _variant_by_id(connection: sqlite3.Connection, variant_id: str) -> HypothesisVariant:
    db = connection
    row = db.execute("SELECT * FROM hypothesis_variants WHERE variant_id = ?", (variant_id,)).fetchone()
    if row is None:
        raise SemanticIdentityError("Hypothesis variant does not exist.")
    return _variant(row)


def _variant(row: sqlite3.Row, *, offset: int = 0) -> HypothesisVariant:
    values = row
    return HypothesisVariant(
        variant_id=str(values[offset + 0]),
        semantic_fingerprint=str(values[offset + 1]),
        capital_kind=CapitalReferenceKind(str(values[offset + 2])),
        capital_reference=str(values[offset + 3]),
        availability=str(values[offset + 4]),
        direction=str(values[offset + 5]),
        horizon_class=str(values[offset + 6]),
        theme_key=None if values[offset + 7] is None else str(values[offset + 7]),
        causal_mechanisms=tuple(str(item) for item in json.loads(str(values[offset + 8]))),
        regime_assumptions=tuple(str(item) for item in json.loads(str(values[offset + 9]))),
        created_at=datetime.fromisoformat(str(values[offset + 10])),
    )


def _successor_task_bindings(
    connection: sqlite3.Connection,
    predecessor_job_ids: tuple[str, ...],
    supplied: tuple[ResearchJobTaskBinding, ...],
) -> tuple[ResearchJobTaskBinding, ...]:
    """Form a complete successor task union with explicit reuse provenance."""
    db = connection
    by_semantics: dict[str, ResearchJobTaskBinding] = {}
    for binding in sorted(supplied, key=lambda item: item.task_id):
        semantic_key = _task_semantic_key(db, binding.task_id)
        existing = by_semantics.get(semantic_key)
        if existing is not None:
            raise SemanticTransitionError("Successor task bindings must be unique by task semantics.")
        by_semantics[semantic_key] = binding
    for predecessor_job_id in sorted(predecessor_job_ids):
        predecessor = db.execute(
            "SELECT case_id FROM research_job_semantics WHERE job_id = ?",
            (predecessor_job_id,),
        ).fetchone()
        if predecessor is None:
            raise SemanticTransitionError("Every research predecessor requires a semantic binding.")
        rows = db.execute(
            """SELECT task_id, role, execution_status, completed_at
            FROM research_job_tasks WHERE job_id = ? ORDER BY task_id""",
            (predecessor_job_id,),
        ).fetchall()
        for row in rows:
            task_id = str(row[0])
            semantic_key = _task_semantic_key(db, task_id)
            origins = tuple(
                str(origin[0])
                for origin in db.execute(
                    """SELECT unit_id FROM research_job_task_origins
                    WHERE job_id = ? AND task_id = ? ORDER BY unit_id""",
                    (predecessor_job_id, task_id),
                ).fetchall()
            )
            existing = by_semantics.get(semantic_key)
            combined_origins = tuple(sorted(set(origins).union(() if existing is None else existing.origin_unit_ids)))
            by_semantics[semantic_key] = _merged_predecessor_task_binding(
                existing=existing,
                predecessor_job_id=predecessor_job_id,
                predecessor_task_id=task_id,
                predecessor_role=ResearchTaskRole(str(row[1])),
                predecessor_status=ResearchTaskExecutionStatus(str(row[2])),
                predecessor_completed_at=None if row[3] is None else datetime.fromisoformat(str(row[3])),
                combined_origins=combined_origins,
            )
    return tuple(sorted(by_semantics.values(), key=lambda binding: binding.task_id))


def _merged_predecessor_task_binding(
    *,
    existing: ResearchJobTaskBinding | None,
    predecessor_job_id: str,
    predecessor_task_id: str,
    predecessor_role: ResearchTaskRole,
    predecessor_status: ResearchTaskExecutionStatus,
    predecessor_completed_at: datetime | None,
    combined_origins: tuple[str, ...],
) -> ResearchJobTaskBinding:
    completed = predecessor_status in {
        ResearchTaskExecutionStatus.COMPLETED,
        ResearchTaskExecutionStatus.REUSED,
    }
    if completed and predecessor_completed_at is None:
        raise SemanticTransitionError("A completed predecessor task requires its completion time.")
    if completed and (
        existing is None
        or existing.execution_status not in {ResearchTaskExecutionStatus.COMPLETED, ResearchTaskExecutionStatus.REUSED}
    ):
        return ResearchJobTaskBinding(
            task_id=predecessor_task_id if existing is None else existing.task_id,
            role=predecessor_role if existing is None else existing.role,
            execution_status=ResearchTaskExecutionStatus.REUSED,
            origin_unit_ids=combined_origins,
            completed_at=predecessor_completed_at,
            reused_from_job_id=predecessor_job_id,
            reused_from_task_id=predecessor_task_id,
        )
    if existing is not None:
        return existing.model_copy(update={"origin_unit_ids": combined_origins})
    return ResearchJobTaskBinding(
        task_id=predecessor_task_id,
        role=predecessor_role,
        origin_unit_ids=combined_origins,
    )


def _task_semantic_key(connection: sqlite3.Connection, task_id: str) -> str:
    row = connection.execute("SELECT task_json FROM planned_research_tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise SemanticTransitionError("Research task semantic binding requires an immutable planned task.")
    try:
        payload = _TASK_PAYLOAD_ADAPTER.validate_json(str(row[0]))
        return research_task_semantics_from_mapping(payload).model_dump_json()
    except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise SemanticTransitionError("Research task semantic payload is malformed.") from error


def _bind_job_task(connection: sqlite3.Connection, job_id: str, binding: ResearchJobTaskBinding) -> None:
    db = connection
    payload = {
        "reused_from_job_id": binding.reused_from_job_id,
        "reused_from_task_id": binding.reused_from_task_id,
    }
    _ = db.execute(
        """INSERT INTO research_job_tasks
        (job_id, task_id, role, execution_status, materialized_session_id,
         materialized_task_id, completed_at, reused_from_job_id, reused_from_task_id, binding_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_id, task_id) DO NOTHING""",
        (
            job_id,
            binding.task_id,
            binding.role.value,
            binding.execution_status.value,
            binding.materialized_session_id,
            binding.materialized_task_id,
            None if binding.completed_at is None else _time(binding.completed_at),
            binding.reused_from_job_id,
            binding.reused_from_task_id,
            _json(payload),
        ),
    )
    for unit_id in binding.origin_unit_ids:
        _ = db.execute(
            "INSERT OR IGNORE INTO research_job_task_origins (job_id, task_id, unit_id) VALUES (?, ?, ?)",
            (job_id, binding.task_id, unit_id),
        )


def _task_binding(connection: sqlite3.Connection, row: sqlite3.Row) -> ResearchJobTaskBinding:
    db = connection
    values = row
    origins = db.execute(
        """SELECT unit_id FROM research_job_task_origins
        WHERE job_id = ? AND task_id = ? ORDER BY unit_id""",
        (str(values["job_id"]), str(values["task_id"])),
    ).fetchall()
    return ResearchJobTaskBinding(
        task_id=str(values["task_id"]),
        role=ResearchTaskRole(str(values["role"])),
        execution_status=ResearchTaskExecutionStatus(str(values["execution_status"])),
        origin_unit_ids=tuple(str(item[0]) for item in origins),
        materialized_session_id=None
        if values["materialized_session_id"] is None
        else str(values["materialized_session_id"]),
        materialized_task_id=None if values["materialized_task_id"] is None else str(values["materialized_task_id"]),
        completed_at=None if values["completed_at"] is None else datetime.fromisoformat(str(values["completed_at"])),
        reused_from_job_id=None if values["reused_from_job_id"] is None else str(values["reused_from_job_id"]),
        reused_from_task_id=None if values["reused_from_task_id"] is None else str(values["reused_from_task_id"]),
    )


def _supersede_job(connection: sqlite3.Connection, predecessor: str, successor: str) -> None:
    db = connection
    predecessor_row = db.execute(
        "SELECT case_id, disposition FROM research_job_semantics WHERE job_id = ?", (predecessor,)
    ).fetchone()
    successor_row = db.execute("SELECT case_id FROM research_job_semantics WHERE job_id = ?", (successor,)).fetchone()
    if predecessor_row is None:
        raise SemanticTransitionError("Research predecessor has no semantic binding.")
    if successor_row is not None and str(successor_row[0]) != str(predecessor_row[0]):
        raise SemanticTransitionError("Research successor belongs to a different semantic case.")
    _ = db.execute(
        """UPDATE research_job_semantics SET disposition = 'superseded', successor_job_id = ?
        WHERE job_id = ? AND disposition = 'current'""",
        (successor, predecessor),
    )
    _ = db.execute(
        """UPDATE synthesis_unit_semantics
        SET disposition = 'superseded', successor_unit_id = NULL,
            successor_research_job_id = ?
        WHERE disposition = 'current' AND material_state_id IN (
          SELECT material_state_id FROM synthesis_material_origins
          WHERE research_job_id = ?)""",
        (successor, predecessor),
    )
    _ = db.execute(
        """UPDATE research_cases SET head_job_id = NULL
        WHERE case_id = ? AND head_job_id = ?""",
        (str(predecessor_row[0]), predecessor),
    )


def _job_semantic(connection: sqlite3.Connection, row: sqlite3.Row) -> ResearchJobSemanticRecord:
    db = connection
    values = row
    case = db.execute(
        "SELECT hypothesis_id, scope_fingerprint FROM research_cases WHERE case_id = ?", (str(values["case_id"]),)
    ).fetchone()
    if case is None:
        raise SemanticIdentityError("Research semantic binding has no case.")
    predecessors = db.execute(
        "SELECT predecessor_job_id FROM research_job_predecessors WHERE successor_job_id = ? ORDER BY predecessor_job_id",
        (str(values["job_id"]),),
    ).fetchall()
    return ResearchJobSemanticRecord(
        job_id=str(values["job_id"]),
        case_id=str(values["case_id"]),
        hypothesis_id=str(case[0]),
        scope_fingerprint=str(case[1]),
        semantic_premise_fingerprint=str(values["semantic_premise_fingerprint"]),
        predecessor_job_ids=tuple(str(item[0]) for item in predecessors),
        successor_job_id=None if values["successor_job_id"] is None else str(values["successor_job_id"]),
        disposition=ResearchJobDisposition(str(values["disposition"])),
        recorded_at=datetime.fromisoformat(str(values["recorded_at"])),
    )


def _material_state(connection: sqlite3.Connection, row: sqlite3.Row) -> SynthesisMaterialState:
    db = connection
    values = row
    origin_rows = db.execute(
        "SELECT research_job_id FROM synthesis_material_origins WHERE material_state_id = ? ORDER BY research_job_id",
        (str(values["material_state_id"]),),
    ).fetchall()
    return SynthesisMaterialState(
        material_state_id=str(values["material_state_id"]),
        hypothesis_id=str(values["hypothesis_id"]),
        material_fingerprint=str(values["material_fingerprint"]),
        eligibility=SynthesisEligibility(str(values["eligibility"])),
        prior_revision_id=None if values["prior_revision_id"] is None else str(values["prior_revision_id"]),
        created_at=datetime.fromisoformat(str(values["created_at"])),
        assessment=cast("JsonValue", json.loads(str(values["assessment_json"]))),
        research_job_ids=tuple(str(item[0]) for item in origin_rows),
    )


def _source_candidate_ids(connection: sqlite3.Connection, source_id: str | None) -> set[str]:
    if source_id is None:
        return set()
    db = connection
    rows = db.execute(
        """SELECT DISTINCT origin.candidate_thesis_id FROM candidate_discovery_origins origin
        JOIN discovery_units unit ON unit.unit_id = origin.unit_id WHERE unit.source_id = ?""",
        (source_id,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _semantic_disposition_counts(connection: sqlite3.Connection, table: str, source_id: str | None) -> dict[str, int]:
    db = connection
    if table == "research_job_semantics":
        if source_id is None:
            rows = db.execute(
                "SELECT disposition, count(*) FROM research_job_semantics GROUP BY disposition"
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT semantic.disposition, count(DISTINCT semantic.job_id)
                FROM research_job_semantics semantic
                JOIN research_job_discovery_origins origin ON origin.job_id = semantic.job_id
                JOIN discovery_units unit ON unit.unit_id = origin.unit_id
                WHERE unit.source_id = ? GROUP BY semantic.disposition""",
                (source_id,),
            ).fetchall()
    elif table == "synthesis_unit_semantics":
        if source_id is None:
            rows = db.execute(
                "SELECT disposition, count(*) FROM synthesis_unit_semantics GROUP BY disposition"
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT semantic.disposition, count(DISTINCT semantic.unit_id)
                FROM synthesis_unit_semantics semantic
                JOIN synthesis_material_origins material_origin USING (material_state_id)
                JOIN research_job_discovery_origins discovery_origin
                  ON discovery_origin.job_id = material_origin.research_job_id
                JOIN discovery_units unit ON unit.unit_id = discovery_origin.unit_id
                WHERE unit.source_id = ? AND NOT EXISTS (
                  SELECT 1 FROM synthesis_material_origins other_material_origin
                  JOIN research_job_discovery_origins other_discovery_origin
                    ON other_discovery_origin.job_id = other_material_origin.research_job_id
                  JOIN discovery_units other_unit ON other_unit.unit_id = other_discovery_origin.unit_id
                  WHERE other_material_origin.material_state_id = semantic.material_state_id
                    AND (other_unit.source_id IS NULL OR other_unit.source_id != ?))
                GROUP BY semantic.disposition""",
                (source_id, source_id),
            ).fetchall()
    else:
        raise SemanticIdentityError("Unknown semantic lifecycle table.")
    return {str(row[0]): int(row[1]) for row in rows}


def _lineage(connection: sqlite3.Connection, durable_id: str) -> SemanticLineage:
    """Project complete variant/group lineage without mutating durable state."""
    db = connection
    review_models: tuple[HypothesisReview, ...] = ()
    if durable_id.startswith("candidate:"):
        candidate_ids = (durable_id,)
        row = db.execute(
            "SELECT variant_id FROM candidate_hypothesis_memberships WHERE candidate_thesis_id = ?",
            (durable_id,),
        ).fetchone()
        group_ids = () if row is None else (_effective_group_for_variant(db, str(row[0])),)
        kind = "candidate"
    elif durable_id.startswith("hypothesis-group:"):
        group_ids = (_effective_group_id(db, durable_id),)
        candidate_ids = _candidate_ids_for_groups(db, group_ids)
        kind = "hypothesis_group"
    elif durable_id.startswith("hypothesis-variant:"):
        group_ids = (_effective_group_for_variant(db, durable_id),)
        candidate_ids = _candidate_ids_for_groups(db, group_ids)
        kind = "hypothesis_variant"
    elif durable_id.startswith("hypothesis-review:"):
        row = db.execute(
            """SELECT review.*, resolution.decision, resolution.resolved_at,
            resolution.actor, resolution.reason FROM hypothesis_reviews review
            LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
            WHERE review.review_id = ?""",
            (durable_id,),
        ).fetchone()
        if row is None:
            raise SemanticIdentityError("Hypothesis review does not exist.")
        review_models = (_review(row),)
        candidate_ids = (review_models[0].subject_candidate_id, review_models[0].comparison_candidate_id)
        group_ids = tuple(
            sorted(
                {
                    _effective_group_for_variant(db, review_models[0].subject_variant_id),
                    _effective_group_for_variant(db, review_models[0].comparison_variant_id),
                }
            )
        )
        kind = "hypothesis_review"
    elif durable_id.startswith("research-job:"):
        row = db.execute(
            """SELECT research_case.hypothesis_id FROM research_job_semantics semantic
            JOIN research_cases research_case USING (case_id) WHERE semantic.job_id = ?""",
            (durable_id,),
        ).fetchone()
        group_ids = () if row is None else (_effective_group_id(db, str(row[0])),)
        candidate_ids = _candidate_ids_for_groups(db, group_ids)
        kind = "research_job"
    elif durable_id.startswith("synthesis-unit:"):
        row = db.execute(
            """SELECT material.hypothesis_id FROM synthesis_unit_semantics semantic
            JOIN synthesis_material_states material USING (material_state_id)
            WHERE semantic.unit_id = ?""",
            (durable_id,),
        ).fetchone()
        group_ids = () if row is None else (_effective_group_id(db, str(row[0])),)
        candidate_ids = _candidate_ids_for_groups(db, group_ids)
        kind = "synthesis_unit"
    else:
        raise SemanticIdentityError("Lineage requires a namespace-qualified durable identifier.")
    variant_ids = tuple(
        sorted({variant_id for group_id in group_ids for variant_id in _group_variant_ids(db, group_id)})
    )
    variants = tuple(_variant_by_id(db, variant_id) for variant_id in variant_ids)
    groups = tuple(_group(db, group_id) for group_id in group_ids)
    memberships = tuple(_membership_lineage(db, candidate_id) for candidate_id in candidate_ids)
    if not review_models:
        review_models = _reviews_for_candidates(db, candidate_ids)
    jobs = tuple(
        str(row[0])
        for group_id in group_ids
        for row in db.execute(
            """SELECT semantic.job_id FROM research_job_semantics semantic
            JOIN research_cases research_case USING (case_id)
            WHERE research_case.hypothesis_id = ? ORDER BY semantic.job_id""",
            (group_id,),
        ).fetchall()
    )
    research = tuple(
        ResearchJobLineage(
            semantic=_job_semantic(db, row),
            tasks=tuple(
                _task_binding(db, task_row)
                for task_row in db.execute(
                    "SELECT * FROM research_job_tasks WHERE job_id = ? ORDER BY role, task_id", (job_id,)
                ).fetchall()
            ),
        )
        for job_id in jobs
        for row in (db.execute("SELECT * FROM research_job_semantics WHERE job_id = ?", (job_id,)).fetchone(),)
        if row is not None
    )
    synthesis_ids = tuple(
        str(row[0])
        for group_id in group_ids
        for row in db.execute(
            """SELECT semantic.unit_id FROM synthesis_unit_semantics semantic
            JOIN synthesis_material_states material USING (material_state_id)
            WHERE material.hypothesis_id = ? ORDER BY semantic.unit_id""",
            (group_id,),
        ).fetchall()
    )
    synthesis = tuple(_synthesis_lineage(db, unit_id) for unit_id in synthesis_ids)
    origins = tuple(sorted({origin for membership in memberships for origin in membership.discovery_origin_unit_ids}))
    return SemanticLineage(
        durable_id=durable_id,
        durable_kind=kind,
        hypothesis_ids=group_ids,
        candidate_ids=candidate_ids,
        research_job_ids=jobs,
        task_ids=tuple(sorted({task.task_id for item in research for task in item.tasks})),
        synthesis_unit_ids=synthesis_ids,
        origin_unit_ids=origins,
        variants=variants,
        groups=groups,
        memberships=memberships,
        reviews=review_models,
        research_jobs=research,
        synthesis=synthesis,
    )


def _candidate_ids_for_groups(connection: sqlite3.Connection, group_ids: tuple[str, ...]) -> tuple[str, ...]:
    db = connection
    rows = db.execute(
        "SELECT candidate_thesis_id, variant_id FROM candidate_hypothesis_memberships ORDER BY candidate_thesis_id"
    ).fetchall()
    return tuple(str(row[0]) for row in rows if _effective_group_for_variant(db, str(row[1])) in group_ids)


def _synthesis_lineage(connection: sqlite3.Connection, unit_id: str) -> SynthesisUnitLineage:
    row = connection.execute(
        """SELECT semantic.*, output.output_record_ids_json
        FROM synthesis_unit_semantics semantic
        LEFT JOIN synthesis_outputs output USING (unit_id)
        WHERE semantic.unit_id = ?""",
        (unit_id,),
    ).fetchone()
    if row is None:
        raise SemanticIdentityError("Synthesis unit has no semantic lineage.")
    material_row = connection.execute(
        "SELECT * FROM synthesis_material_states WHERE material_state_id = ?",
        (str(row["material_state_id"]),),
    ).fetchone()
    if material_row is None:
        raise SemanticIdentityError("Synthesis semantic lineage has no material state.")
    output_record_ids = (
        ()
        if row["output_record_ids_json"] is None
        else tuple(_STRING_LIST_ADAPTER.validate_json(str(row["output_record_ids_json"])))
    )
    return SynthesisUnitLineage(
        material_state=_material_state(connection, material_row),
        unit_id=unit_id,
        disposition=SynthesisDisposition(str(row["disposition"])),
        successor_unit_id=None if row["successor_unit_id"] is None else str(row["successor_unit_id"]),
        successor_research_job_id=None
        if row["successor_research_job_id"] is None
        else str(row["successor_research_job_id"]),
        output_record_ids=output_record_ids,
        thesis_revision_ids=tuple(
            item.removeprefix("thesis_revision:") for item in output_record_ids if item.startswith("thesis_revision:")
        ),
    )


def _group(connection: sqlite3.Connection, group_id: str) -> CanonicalHypothesisGroup:
    db = connection
    row = db.execute(
        "SELECT status, created_at FROM canonical_hypothesis_groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is None:
        raise SemanticIdentityError("Canonical hypothesis group does not exist.")
    return CanonicalHypothesisGroup(
        group_id=group_id,
        variant_ids=_group_variant_ids(db, group_id),
        status=str(row[0]),
        created_at=datetime.fromisoformat(str(row[1])),
    )


def _membership_lineage(connection: sqlite3.Connection, candidate_id: str) -> CandidateHypothesisLineage:
    db = connection
    row = db.execute(
        """SELECT variant_id, membership_kind FROM candidate_hypothesis_memberships
        WHERE candidate_thesis_id = ?""",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise SemanticIdentityError("Candidate has no hypothesis variant membership.")
    origins = tuple(
        str(item[0])
        for item in db.execute(
            "SELECT unit_id FROM candidate_discovery_origins WHERE candidate_thesis_id = ? ORDER BY unit_id",
            (candidate_id,),
        ).fetchall()
    )
    return CandidateHypothesisLineage(
        candidate_thesis_id=candidate_id,
        variant_id=str(row[0]),
        group_id=_effective_group_for_variant(db, str(row[0])),
        membership_kind=HypothesisMembershipKind(str(row[1])),
        discovery_origin_unit_ids=origins,
    )


def _reviews_for_candidates(
    connection: sqlite3.Connection, candidate_ids: tuple[str, ...]
) -> tuple[HypothesisReview, ...]:
    db = connection
    if not candidate_ids:
        return ()
    rows = db.execute(
        """SELECT review.*, resolution.decision, resolution.resolved_at,
        resolution.actor, resolution.reason FROM hypothesis_reviews review
        LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
        WHERE review.subject_candidate_id IN (SELECT value FROM json_each(?))
           OR review.comparison_candidate_id IN (SELECT value FROM json_each(?))
        ORDER BY review.created_at, review.review_id""",
        (_json(candidate_ids), _json(candidate_ids)),
    ).fetchall()
    return tuple(_review(row) for row in rows)
