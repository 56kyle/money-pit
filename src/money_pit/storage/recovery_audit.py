"""Provider-free invariant audit for durable incremental intelligence work."""

# pyright: reportAny=false

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import cast

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import JsonValue
from pydantic import TypeAdapter
from pydantic import ValidationError

from money_pit.contracts import CandidateResearchSummary
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.theses import CandidateThesis


if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from money_pit.storage.database import Database


_FROZEN = ConfigDict(frozen=True, extra="forbid")
_CLAIM_LEASE = timedelta(minutes=30)
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _checkpoint_digest_is_reconstructable(candidate_id: str, digest_json: str) -> bool:
    try:
        digest = _JSON_VALUE_ADAPTER.validate_json(digest_json)
        if not isinstance(digest, dict) or "backfilled_session_id" in digest:
            return False
        typed_digest = cast("dict[str, JsonValue]", digest)
        candidate = CandidateResearchSummary.model_validate(typed_digest.get("candidate"))
        contexts = typed_digest.get("contexts")
        if candidate.candidate_thesis_id != candidate_id:
            return False
        if not isinstance(contexts, list):
            return False
        typed_contexts = cast("list[JsonValue]", contexts)
        _ = tuple(ResearchCumulativeContext.model_validate(value) for value in typed_contexts)
    except (ValidationError, TypeError):
        return False
    return True


def _provider_wave_rows(
    connection: sqlite3.Connection,
    source_id: str | None,
) -> Sequence[sqlite3.Row]:
    """Return provider-complete waves in the requested source scope."""
    if source_id is None:
        return connection.execute(
            """SELECT wave_result_id FROM research_wave_results
            WHERE phase = 'provider_completed' ORDER BY wave_result_id"""
        ).fetchall()
    return connection.execute(
        """SELECT wave.wave_result_id FROM research_wave_results wave
        WHERE wave.phase = 'provider_completed' AND EXISTS (
            SELECT 1 FROM research_job_discovery_origins origin
            JOIN discovery_units unit ON unit.unit_id = origin.unit_id
            WHERE origin.job_id = wave.job_id AND unit.source_id = ?)
        ORDER BY wave.wave_result_id""",
        (source_id,),
    ).fetchall()


def _execution_wave_rows(
    connection: sqlite3.Connection,
    source_id: str | None,
) -> Sequence[sqlite3.Row]:
    """Return uncheckpointed execution-complete waves in the requested source scope."""
    if source_id is None:
        return connection.execute(
            """SELECT wave_result_id FROM research_wave_results
            WHERE phase = 'execution_completed' AND checkpointed_at IS NULL
            ORDER BY wave_result_id"""
        ).fetchall()
    return connection.execute(
        """SELECT wave.wave_result_id FROM research_wave_results wave
        WHERE wave.phase = 'execution_completed' AND wave.checkpointed_at IS NULL
          AND EXISTS (
            SELECT 1 FROM research_job_discovery_origins origin
            JOIN discovery_units unit ON unit.unit_id = origin.unit_id
            WHERE origin.job_id = wave.job_id AND unit.source_id = ?)
        ORDER BY wave.wave_result_id""",
        (source_id,),
    ).fetchall()


class RecoveryAuditDisposition(StrEnum):
    """Operator significance of one recovery audit finding."""

    RESUMABLE = "resumable"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


class RecoveryAuditStatus(StrEnum):
    """Overall provider-free recovery state."""

    HEALTHY = "healthy"
    RESUMABLE = "resumable"
    BLOCKED = "blocked"


class RecoveryAuditFinding(BaseModel):
    """One typed invariant or recovery finding."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    code: str
    disposition: RecoveryAuditDisposition
    detail: str
    durable_ids: tuple[str, ...] = ()


class RecoveryAuditReport(BaseModel):
    """Read-only determination of whether provider-backed updates may proceed."""

    model_config: ClassVar[ConfigDict] = _FROZEN
    status: RecoveryAuditStatus
    source_id: str | None
    checked_at: datetime
    findings: tuple[RecoveryAuditFinding, ...]

    @property
    def blocked(self) -> bool:
        """Return whether an invariant violation forbids provider construction."""
        return self.status is RecoveryAuditStatus.BLOCKED


class RecoveryAuditBlockedError(Exception):
    """Raised before provider construction when durable recovery is malformed."""

    def __init__(self, report: RecoveryAuditReport) -> None:
        """Retain the blocked report without exposing provider state."""
        self.report: RecoveryAuditReport = report
        super().__init__("Durable intelligence recovery audit found blocked work.")


class RecoveryAuditor:
    """Validate incremental lifecycle state through a read-only database handle."""

    def __init__(self, database: Database) -> None:
        """Bind the auditor to one existing database path."""
        self._database: Database = database

    def audit(self, *, source_id: str | None = None) -> RecoveryAuditReport:
        """Return typed healthy, resumable, and blocked findings without repair."""
        checked_at = datetime.now(tz=timezone.utc)
        findings: list[RecoveryAuditFinding] = []
        with self._database.read_only_transaction() as connection:
            provider_rows = _provider_wave_rows(connection, source_id)
            if provider_rows:
                provider_ids = {str(row[0]) for row in provider_rows}
                findings.append(
                    RecoveryAuditFinding(
                        code="provider_wave_awaiting_interpretation",
                        disposition=RecoveryAuditDisposition.RESUMABLE,
                        detail="Provider work is durable and can resume semantic interpretation without repeating I/O.",
                        durable_ids=tuple(str(row[0]) for row in provider_rows),
                    )
                )
                reusable_rows = connection.execute(
                    """SELECT wave.wave_result_id FROM research_wave_results wave
                    WHERE wave.phase = 'provider_completed'
                      AND json_array_length(json_extract(wave.provider_result_json, '$.asset_ids')) > 0
                      AND NOT EXISTS (
                        SELECT 1 FROM json_each(wave.provider_result_json, '$.asset_ids') asset
                        WHERE NOT EXISTS (
                          SELECT 1 FROM claim_interpretation_attempts attempt
                          WHERE attempt.asset_id = asset.value AND attempt.outcome = 'succeeded'))
                    ORDER BY wave.wave_result_id"""
                ).fetchall()
                reusable_ids = {str(row[0]) for row in reusable_rows} & provider_ids
                if reusable_ids:
                    findings.append(
                        RecoveryAuditFinding(
                            code="exact_interpretation_reuse_available",
                            disposition=RecoveryAuditDisposition.RESUMABLE,
                            detail="Every provider-wave asset has a successful durable interpretation available for reuse.",
                            durable_ids=tuple(sorted(reusable_ids)),
                        )
                    )
                unavailable_ids = tuple(
                    sorted(identifier for identifier in provider_ids if identifier not in reusable_ids)
                )
                if unavailable_ids:
                    findings.append(
                        RecoveryAuditFinding(
                            code="interpretation_reuse_unavailable",
                            disposition=RecoveryAuditDisposition.RESUMABLE,
                            detail="Provider work is durable, but at least one asset still requires interpretation.",
                            durable_ids=unavailable_ids,
                        )
                    )
            execution_rows = _execution_wave_rows(connection, source_id)
            if execution_rows:
                findings.append(
                    RecoveryAuditFinding(
                        code="execution_wave_awaiting_checkpoint",
                        disposition=RecoveryAuditDisposition.RESUMABLE,
                        detail="Completed semantic work is durable and awaits atomic checkpoint consumption.",
                        durable_ids=tuple(str(row[0]) for row in execution_rows),
                    )
                )
            malformed_waves = connection.execute(
                """SELECT wave_result_id FROM research_wave_results
                WHERE (phase = 'provider_completed' AND execution_completed_run_id IS NOT NULL)
                   OR (phase = 'execution_completed' AND execution_completed_run_id IS NULL)
                   OR (checkpointed_at IS NULL) != (checkpointed_run_id IS NULL)
                ORDER BY wave_result_id"""
            ).fetchall()
            _blocked_rows(
                findings,
                "malformed_research_wave",
                "Research wave phase and transition ownership disagree.",
                malformed_waves,
            )
            counter_rows = connection.execute(
                """SELECT checkpoint.checkpoint_id
                FROM research_job_checkpoints checkpoint
                WHERE checkpoint.search_count != COALESCE((
                    SELECT sum(CAST(json_extract(wave.execution_json, '$.search_count_delta') AS INTEGER))
                    FROM research_wave_results wave
                    WHERE wave.job_id = checkpoint.job_id AND wave.checkpointed_at IS NOT NULL
                      AND wave.wave_number <= checkpoint.wave_number), 0)
                   OR checkpoint.accepted_fetch_count != COALESCE((
                    SELECT sum(CAST(json_extract(wave.execution_json, '$.accepted_fetch_count_delta') AS INTEGER))
                    FROM research_wave_results wave
                    WHERE wave.job_id = checkpoint.job_id AND wave.checkpointed_at IS NOT NULL
                      AND wave.wave_number <= checkpoint.wave_number), 0)
                ORDER BY checkpoint.checkpoint_id"""
            ).fetchall()
            _blocked_rows(
                findings,
                "research_checkpoint_counter_mismatch",
                "Checkpoint counters do not equal the consumed immutable wave deltas.",
                counter_rows,
            )
            active_rows = connection.execute(
                """SELECT kind, work_id, claimed_run_id, claimed_at FROM (
                    SELECT 'interpretation_chunk' kind, chunk_id work_id, claimed_run_id, claimed_at
                    FROM interpretation_bundle_chunks WHERE status = 'active'
                    UNION ALL SELECT 'research_job', job_id, claimed_run_id, claimed_at
                    FROM research_jobs WHERE status = 'active'
                    UNION ALL SELECT 'synthesis_unit', unit_id, claimed_run_id, claimed_at
                    FROM synthesis_units WHERE status = 'active') active
                WHERE EXISTS (SELECT 1 FROM run_terminal_events terminal
                              WHERE terminal.run_id = active.claimed_run_id)
                   OR active.claimed_at <= ? ORDER BY kind, work_id""",
                ((checked_at - _CLAIM_LEASE).isoformat(),),
            ).fetchall()
            active_batches = connection.execute(
                """SELECT batch_id FROM discovery_batches batch
                WHERE status = 'active' AND EXISTS (
                    SELECT 1 FROM run_terminal_events terminal WHERE terminal.run_id = batch.run_id)
                ORDER BY batch_id"""
            ).fetchall()
            active_rows.extend(("discovery_batch", str(row[0]), None, None) for row in active_batches)
            if active_rows:
                findings.append(
                    RecoveryAuditFinding(
                        code="reclaimable_work_claim",
                        disposition=RecoveryAuditDisposition.RESUMABLE,
                        detail="An expired or terminal-run claim can be safely adopted by a later run.",
                        durable_ids=tuple(str(row[1]) for row in active_rows),
                    )
                )
            malformed_calls = connection.execute(
                """SELECT call.call_id FROM inference_calls call
                LEFT JOIN runs run ON run.run_id = call.run_id
                WHERE run.run_id IS NULL OR call.work_unit_id = '' OR
                  (call.stage = 'A1' AND NOT EXISTS (
                    SELECT 1 FROM claim_interpretation_attempts attempt
                    WHERE attempt.attempt_id = call.work_unit_id)
                   AND NOT EXISTS (SELECT 1 FROM interpretation_bundle_chunks chunk
                                   WHERE chunk.chunk_id = call.work_unit_id)) OR
                  (call.stage = 'A2' AND NOT EXISTS (
                    SELECT 1 FROM discovery_batches batch WHERE batch.batch_id = call.work_unit_id)) OR
                  (call.stage = 'A3' AND NOT EXISTS (
                    SELECT 1 FROM research_jobs job WHERE job.job_id = call.work_unit_id)
                   AND NOT (call.work_unit_id LIKE 'research:candidate:%' AND EXISTS (
                    SELECT 1 FROM candidate_theses candidate
                    WHERE candidate.candidate_thesis_id = substr(call.work_unit_id, 10)))) OR
                  (call.stage = 'A4' AND NOT EXISTS (
                    SELECT 1 FROM synthesis_units unit WHERE unit.unit_id = call.work_unit_id))
                ORDER BY call.call_id"""
            ).fetchall()
            _blocked_rows(
                findings,
                "malformed_inference_correlation",
                "Inference accounting does not identify an existing run and durable work unit.",
                malformed_calls,
            )
            artifact_rows = connection.execute(
                "SELECT artifact_id, output_ids_json, payload_hash, payload_json FROM stage_artifacts"
            ).fetchall()
            malformed_artifacts: list[tuple[str]] = []
            for row in artifact_rows:
                try:
                    outputs = json.loads(str(row[1]))
                except json.JSONDecodeError:
                    outputs = None
                payload_hash = hashlib.sha256(str(row[3]).encode()).hexdigest()
                output_values = () if not isinstance(outputs, list) else cast("list[object]", outputs)
                if (
                    not isinstance(outputs, list)
                    or len(output_values) != len({str(value) for value in output_values})
                    or payload_hash != str(row[2])
                ):
                    malformed_artifacts.append((str(row[0]),))
            _blocked_rows(
                findings,
                "malformed_stage_artifact",
                "A stage artifact has duplicate output bindings or a mismatched payload fingerprint.",
                malformed_artifacts,
            )
            findings.extend(_semantic_findings(connection, source_id))
        status = (
            RecoveryAuditStatus.BLOCKED
            if any(item.disposition is RecoveryAuditDisposition.BLOCKED for item in findings)
            else RecoveryAuditStatus.RESUMABLE
            if findings
            else RecoveryAuditStatus.HEALTHY
        )
        return RecoveryAuditReport(
            status=status,
            source_id=source_id,
            checked_at=checked_at,
            findings=tuple(findings),
        )


def _semantic_findings(
    connection: sqlite3.Connection,
    source_id: str | None,
) -> list[RecoveryAuditFinding]:
    scope = _SemanticAuditScope.load(connection, source_id)
    return [
        *_semantic_identity_findings(connection, scope),
        *_semantic_research_findings(connection, scope),
        *_semantic_synthesis_findings(connection, scope),
    ]


class _SemanticAuditScope:
    """Identifiers causally reachable from one requested source."""

    def __init__(
        self,
        *,
        source_id: str | None,
        candidate_ids: frozenset[str],
        variant_ids: frozenset[str],
        group_ids: frozenset[str],
        job_ids: frozenset[str],
    ) -> None:
        self.source_id: str | None = source_id
        self.candidate_ids: frozenset[str] = candidate_ids
        self.variant_ids: frozenset[str] = variant_ids
        self.group_ids: frozenset[str] = group_ids
        self.job_ids: frozenset[str] = job_ids

    @classmethod
    def load(cls, connection: sqlite3.Connection, source_id: str | None) -> _SemanticAuditScope:
        if source_id is None:
            return cls(
                source_id=None,
                candidate_ids=frozenset(),
                variant_ids=frozenset(),
                group_ids=frozenset(),
                job_ids=frozenset(),
            )
        candidate_ids = frozenset(
            str(row[0])
            for row in connection.execute(
                """SELECT DISTINCT origin.candidate_thesis_id FROM candidate_discovery_origins origin
                JOIN discovery_units unit USING (unit_id) WHERE unit.source_id = ?""",
                (source_id,),
            ).fetchall()
        )
        variant_ids = frozenset(
            str(row[0])
            for row in connection.execute(
                """SELECT DISTINCT variant_id FROM candidate_hypothesis_memberships
                WHERE candidate_thesis_id IN (SELECT value FROM json_each(?))""",
                (json.dumps(sorted(candidate_ids)),),
            ).fetchall()
        )
        group_ids = frozenset(
            str(row[0])
            for row in connection.execute(
                """SELECT DISTINCT group_id FROM canonical_hypothesis_group_variants
                WHERE variant_id IN (SELECT value FROM json_each(?))""",
                (json.dumps(sorted(variant_ids)),),
            ).fetchall()
        )
        job_ids = frozenset(
            str(row[0])
            for row in connection.execute(
                """SELECT DISTINCT origin.job_id FROM research_job_discovery_origins origin
                JOIN discovery_units unit USING (unit_id) WHERE unit.source_id = ?""",
                (source_id,),
            ).fetchall()
        )
        return cls(
            source_id=source_id,
            candidate_ids=candidate_ids,
            variant_ids=variant_ids,
            group_ids=group_ids,
            job_ids=job_ids,
        )

    def contains(self, identifier: str, identifiers: frozenset[str]) -> bool:
        return self.source_id is None or identifier in identifiers


def _semantic_identity_findings(
    connection: sqlite3.Connection,
    scope: _SemanticAuditScope,
) -> list[RecoveryAuditFinding]:
    findings: list[RecoveryAuditFinding] = []
    missing_memberships = connection.execute(
        """SELECT candidate.candidate_thesis_id, candidate.candidate_json,
          EXISTS (SELECT 1 FROM candidate_discovery_origins origin
                  WHERE origin.candidate_thesis_id = candidate.candidate_thesis_id)
        FROM candidate_theses candidate
        LEFT JOIN candidate_hypothesis_memberships membership USING (candidate_thesis_id)
        WHERE membership.candidate_thesis_id IS NULL ORDER BY candidate.candidate_thesis_id"""
    ).fetchall()
    missing_memberships = [row for row in missing_memberships if scope.contains(str(row[0]), scope.candidate_ids)]
    resumable_memberships: list[sqlite3.Row] = []
    malformed_memberships: list[sqlite3.Row] = []
    for row in missing_memberships:
        try:
            _ = CandidateThesis.model_validate_json(str(row[1]))
        except (ValidationError, ValueError):
            malformed_memberships.append(row)
        else:
            (resumable_memberships if bool(row[2]) else malformed_memberships).append(row)
    _finding_rows(
        findings,
        "hypothesis_membership_awaiting_reconciliation",
        RecoveryAuditDisposition.RESUMABLE,
        "A valid immutable proposal with exact discovery origins can be reconciled locally.",
        resumable_memberships,
    )
    _blocked_rows(
        findings,
        "missing_hypothesis_membership",
        "A candidate proposal lacks a valid, origin-grounded semantic reconciliation input.",
        malformed_memberships,
    )
    missing_groups = connection.execute(
        """SELECT variant.variant_id FROM hypothesis_variants variant
        LEFT JOIN canonical_hypothesis_group_variants membership USING (variant_id)
        WHERE membership.variant_id IS NULL ORDER BY variant.variant_id"""
    ).fetchall()
    missing_groups = [row for row in missing_groups if scope.contains(str(row[0]), scope.variant_ids)]
    _blocked_rows(
        findings,
        "missing_hypothesis_group_membership",
        "A semantic variant has no canonical hypothesis group.",
        missing_groups,
    )
    malformed_groups = connection.execute(
        """SELECT group_record.group_id FROM canonical_hypothesis_groups group_record
        WHERE NOT EXISTS (SELECT 1 FROM canonical_hypothesis_group_variants membership
                          WHERE membership.group_id = group_record.group_id)
           OR (group_record.status = 'current' AND EXISTS (
                 SELECT 1 FROM canonical_hypothesis_group_supersessions edge
                 WHERE edge.predecessor_group_id = group_record.group_id))
           OR (group_record.status = 'superseded' AND NOT EXISTS (
                 SELECT 1 FROM canonical_hypothesis_group_supersessions edge
                 WHERE edge.predecessor_group_id = group_record.group_id))
        ORDER BY group_record.group_id"""
    ).fetchall()
    malformed_groups = [row for row in malformed_groups if scope.contains(str(row[0]), scope.group_ids)]
    cycle_rows = connection.execute(
        """WITH RECURSIVE reach(start_group_id, group_id) AS (
          SELECT predecessor_group_id, successor_group_id
          FROM canonical_hypothesis_group_supersessions
          UNION
          SELECT reach.start_group_id, edge.successor_group_id FROM reach
          JOIN canonical_hypothesis_group_supersessions edge
            ON edge.predecessor_group_id = reach.group_id)
        SELECT DISTINCT start_group_id FROM reach WHERE start_group_id = group_id ORDER BY start_group_id"""
    ).fetchall()
    malformed_groups.extend(row for row in cycle_rows if scope.contains(str(row[0]), scope.group_ids))
    _blocked_rows(
        findings,
        "malformed_hypothesis_group_supersession",
        "Canonical group membership, lifecycle, or supersession is malformed.",
        sorted({(str(row[0]),) for row in malformed_groups}),
    )
    unavailable_variants = connection.execute(
        "SELECT variant_id FROM hypothesis_variants WHERE availability = 'unavailable' ORDER BY variant_id"
    ).fetchall()
    _finding_rows(
        findings,
        "unavailable_hypothesis",
        RecoveryAuditDisposition.UNAVAILABLE,
        "A proposal has no authoritative capital reference and cannot enter research.",
        [row for row in unavailable_variants if scope.contains(str(row[0]), scope.variant_ids)],
    )
    pending_reviews = connection.execute(
        """SELECT review.review_id, review.subject_candidate_id, review.comparison_candidate_id
        FROM hypothesis_reviews review
        LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
        WHERE resolution.review_id IS NULL ORDER BY review.review_id"""
    ).fetchall()
    pending_reviews = [
        row
        for row in pending_reviews
        if scope.source_id is None or str(row[1]) in scope.candidate_ids or str(row[2]) in scope.candidate_ids
    ]
    _finding_rows(
        findings,
        "hypothesis_review_required",
        RecoveryAuditDisposition.UNAVAILABLE,
        "Possible semantic duplicates require an operator decision before affected work can run.",
        pending_reviews,
    )
    return findings


def _semantic_research_findings(
    connection: sqlite3.Connection,
    scope: _SemanticAuditScope,
) -> list[RecoveryAuditFinding]:
    findings: list[RecoveryAuditFinding] = []
    orphan_jobs = connection.execute(
        """SELECT job.job_id FROM research_jobs job
        LEFT JOIN research_job_semantics semantic USING (job_id)
        WHERE semantic.job_id IS NULL ORDER BY job.job_id"""
    ).fetchall()
    _blocked_rows(
        findings,
        "research_job_without_semantics",
        "A durable research job has no canonical case or lifecycle disposition.",
        [row for row in orphan_jobs if scope.contains(str(row[0]), scope.job_ids)],
    )
    malformed_heads = connection.execute(
        """SELECT research_case.case_id, research_case.hypothesis_id FROM research_cases research_case
        LEFT JOIN research_job_semantics semantic
          ON semantic.job_id = research_case.head_job_id
         AND semantic.case_id = research_case.case_id
         AND semantic.disposition = 'current'
        WHERE (research_case.head_job_id IS NULL) != NOT EXISTS (
            SELECT 1 FROM research_job_semantics candidate
            WHERE candidate.case_id = research_case.case_id AND candidate.disposition = 'current')
           OR (research_case.head_job_id IS NOT NULL AND semantic.job_id IS NULL)
        ORDER BY research_case.case_id"""
    ).fetchall()
    _blocked_rows(
        findings,
        "malformed_research_head",
        "Research case head and current semantic job disagree.",
        [row for row in malformed_heads if scope.contains(str(row[1]), scope.group_ids)],
    )
    malformed_reuse = connection.execute(
        """SELECT task.job_id || ':' || task.task_id, task.job_id FROM research_job_tasks task
        LEFT JOIN research_job_tasks source
          ON source.job_id = task.reused_from_job_id AND source.task_id = task.reused_from_task_id
        WHERE (task.execution_status = 'reused' AND
               (source.job_id IS NULL OR source.execution_status NOT IN ('completed', 'reused')
                OR source.completed_at != task.completed_at))
           OR (task.execution_status != 'reused' AND
               (task.reused_from_job_id IS NOT NULL OR task.reused_from_task_id IS NOT NULL))
        ORDER BY task.job_id, task.task_id"""
    ).fetchall()
    _blocked_rows(
        findings,
        "malformed_research_task_reuse",
        "A job-scoped task reuse does not identify an exact completed source execution.",
        [row for row in malformed_reuse if scope.contains(str(row[1]), scope.job_ids)],
    )
    unbound_sessions = connection.execute(
        """SELECT session.session_id,
          substr(session.scope_subject_id, instr(session.scope_subject_id, '|research-job:') + 14) job_id
        FROM research_sessions session
        LEFT JOIN research_job_sessions binding USING (session_id)
        WHERE session.scope_kind = 'candidate_thesis'
          AND instr(session.scope_subject_id, '|research-job:') > 0
          AND binding.session_id IS NULL ORDER BY session.session_id"""
    ).fetchall()
    _blocked_rows(
        findings,
        "research_session_without_job_binding",
        "A job-scoped research session has no durable semantic owner.",
        [row for row in unbound_sessions if scope.contains(str(row[1]), scope.job_ids)],
    )
    terminal_without_material = connection.execute(
        """SELECT job.job_id FROM research_jobs job
        JOIN research_job_semantics semantic USING (job_id)
        WHERE job.status = 'terminal' AND semantic.disposition = 'current'
          AND NOT EXISTS (SELECT 1 FROM synthesis_material_origins origin
                          WHERE origin.research_job_id = job.job_id)
          AND NOT EXISTS (SELECT 1 FROM research_job_checkpoints checkpoint
                          WHERE checkpoint.job_id = job.job_id)
        ORDER BY job.job_id"""
    ).fetchall()
    _blocked_rows(
        findings,
        "terminal_research_without_material_assessment",
        "A terminal current research job has no durable synthesis eligibility assessment.",
        [row for row in terminal_without_material if scope.contains(str(row[0]), scope.job_ids)],
    )
    materialization_candidates = connection.execute(
        """SELECT job.job_id, job.candidate_thesis_id, checkpoint.digest_json
        FROM research_jobs job
        JOIN research_job_semantics semantic USING (job_id)
        JOIN research_job_checkpoints checkpoint ON checkpoint.checkpoint_id = (
          SELECT latest.checkpoint_id FROM research_job_checkpoints latest
          WHERE latest.job_id = job.job_id ORDER BY latest.wave_number DESC LIMIT 1)
        WHERE job.status = 'terminal' AND semantic.disposition = 'current'
          AND NOT EXISTS (SELECT 1 FROM synthesis_material_origins origin
                          WHERE origin.research_job_id = job.job_id)
        ORDER BY job.job_id"""
    ).fetchall()
    resumable_material = [
        row for row in materialization_candidates if _checkpoint_digest_is_reconstructable(str(row[1]), str(row[2]))
    ]
    malformed_material = [
        row for row in materialization_candidates if not _checkpoint_digest_is_reconstructable(str(row[1]), str(row[2]))
    ]
    _blocked_rows(
        findings,
        "terminal_research_with_malformed_material_checkpoint",
        "A terminal research checkpoint cannot reconstruct its synthesis eligibility assessment.",
        [row for row in malformed_material if scope.contains(str(row[0]), scope.job_ids)],
    )
    _finding_rows(
        findings,
        "terminal_research_awaiting_materialization",
        RecoveryAuditDisposition.RESUMABLE,
        "A terminal research checkpoint can deterministically materialize its pending synthesis assessment.",
        [row for row in resumable_material if scope.contains(str(row[0]), scope.job_ids)],
    )
    return findings


def _semantic_synthesis_findings(
    connection: sqlite3.Connection,
    scope: _SemanticAuditScope,
) -> list[RecoveryAuditFinding]:
    findings: list[RecoveryAuditFinding] = []
    orphan_units = connection.execute(
        """SELECT unit.unit_id, unit.research_job_id FROM synthesis_units unit
        LEFT JOIN synthesis_unit_semantics semantic USING (unit_id)
        WHERE semantic.unit_id IS NULL ORDER BY unit.unit_id"""
    ).fetchall()
    _blocked_rows(
        findings,
        "synthesis_unit_without_semantics",
        "A durable synthesis unit has no material state or lifecycle disposition.",
        [row for row in orphan_units if scope.contains(str(row[1]), scope.job_ids)],
    )
    malformed_synthesis = connection.execute(
        """SELECT semantic.unit_id, material.hypothesis_id FROM synthesis_unit_semantics semantic
        JOIN synthesis_material_states material USING (material_state_id)
        WHERE (semantic.disposition = 'current' AND material.eligibility != 'eligible')
           OR (semantic.disposition = 'superseded' AND
               ((semantic.successor_unit_id IS NOT NULL) =
                (semantic.successor_research_job_id IS NOT NULL)))
           OR (semantic.disposition != 'superseded' AND
               (semantic.successor_unit_id IS NOT NULL OR semantic.successor_research_job_id IS NOT NULL))
           OR (semantic.successor_research_job_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM research_job_semantics target
               WHERE target.job_id = semantic.successor_research_job_id
                 AND target.disposition = 'current'))
        ORDER BY semantic.unit_id"""
    ).fetchall()
    _blocked_rows(
        findings,
        "malformed_synthesis_semantics",
        "Synthesis disposition conflicts with its material evidence gate or successor.",
        [row for row in malformed_synthesis if scope.contains(str(row[1]), scope.group_ids)],
    )
    ineligible_revisions = connection.execute(
        """SELECT revision.revision_id, thesis.candidate_thesis_id FROM thesis_revisions revision
        JOIN theses thesis ON thesis.thesis_id = revision.thesis_id
        WHERE NOT EXISTS (
            SELECT 1 FROM synthesis_outputs output
            JOIN synthesis_unit_semantics semantic ON semantic.unit_id = output.unit_id
            JOIN synthesis_units unit ON unit.unit_id = semantic.unit_id
            JOIN synthesis_material_states material USING (material_state_id)
            WHERE material.eligibility = 'eligible' AND semantic.disposition = 'current'
              AND unit.status = 'completed'
              AND EXISTS (SELECT 1 FROM json_each(output.output_record_ids_json) identifier
                          WHERE identifier.value = 'thesis_revision:' || revision.revision_id))
        ORDER BY revision.revision_id"""
    ).fetchall()
    _finding_rows(
        findings,
        "thesis_without_eligible_synthesis",
        RecoveryAuditDisposition.UNAVAILABLE,
        "A thesis revision has no eligible synthesis material lineage and cannot enter portfolio review.",
        [row for row in ineligible_revisions if scope.contains(str(row[1]), scope.candidate_ids)],
    )
    return findings


def _finding_rows(
    findings: list[RecoveryAuditFinding],
    code: str,
    disposition: RecoveryAuditDisposition,
    detail: str,
    rows: Sequence[sqlite3.Row | tuple[str]],
) -> None:
    if rows:
        findings.append(
            RecoveryAuditFinding(
                code=code,
                disposition=disposition,
                detail=detail,
                durable_ids=tuple(str(row[0]) for row in rows),
            )
        )


def _blocked_rows(
    findings: list[RecoveryAuditFinding],
    code: str,
    detail: str,
    rows: Sequence[sqlite3.Row | tuple[str]],
) -> None:
    _finding_rows(findings, code, RecoveryAuditDisposition.BLOCKED, detail, rows)
