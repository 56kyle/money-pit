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


if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from money_pit.storage.database import Database


_FROZEN = ConfigDict(frozen=True, extra="forbid")
_CLAIM_LEASE = timedelta(minutes=30)


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
                if not isinstance(outputs, list) or len(output_values) != len(
                    {str(value) for value in output_values}
                ) or payload_hash != str(row[2]):
                    malformed_artifacts.append((str(row[0]),))
            _blocked_rows(
                findings,
                "malformed_stage_artifact",
                "A stage artifact has duplicate output bindings or a mismatched payload fingerprint.",
                malformed_artifacts,
            )
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


def _blocked_rows(
    findings: list[RecoveryAuditFinding],
    code: str,
    detail: str,
    rows: Sequence[sqlite3.Row | tuple[str]],
) -> None:
    if rows:
        findings.append(
            RecoveryAuditFinding(
                code=code,
                disposition=RecoveryAuditDisposition.BLOCKED,
                detail=detail,
                durable_ids=tuple(str(row[0]) for row in rows),
            )
        )
