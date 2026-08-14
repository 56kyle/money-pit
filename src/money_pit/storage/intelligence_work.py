"""Module containing durable incremental intelligence work ledgers."""

# pyright: reportAny=false

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from datetime import timezone
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import cast

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import ValidationError

from money_pit.agents.inference import InferenceCallRecord
from money_pit.agents.inference import InferenceUsage
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


if TYPE_CHECKING:
    import sqlite3


_FROZEN_CONFIG: ConfigDict = ConfigDict(frozen=True, extra="forbid")


class WorkStatus(StrEnum):
    """Lifecycle shared by resumable work units."""

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"


class ResearchJobStatus(StrEnum):
    """Lifecycle of premise-versioned research."""

    PENDING = "pending"
    ACTIVE = "active"
    TERMINAL = "terminal"


class DiscoveryBatchStatus(StrEnum):
    """Lifecycle of a discovery batch with validated-output recovery."""

    ACTIVE = "active"
    CHECKPOINTED = "checkpointed"
    COMPLETED = "completed"


class DiscoveryUnitKind(StrEnum):
    """Material changes eligible for candidate discovery."""

    SOURCE_BUNDLE = "source_bundle"
    CANONICAL_CLAIM = "canonical_claim"
    UNIVERSE_ENTRY = "universe_entry"


class UriDisposition(StrEnum):
    """Admission result for a canonical research URI."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REUSED = "reused"


class IntelligenceWorkError(StorageError):
    """Base class for invalid incremental work state."""


class ImmutableWorkCollisionError(IntelligenceWorkError):
    """Raised when a durable work identity is reused for different content."""


class WorkTransitionError(IntelligenceWorkError):
    """Raised when a work lifecycle transition is invalid."""


class MalformedWorkRecordError(IntelligenceWorkError):
    """Raised when stored work state violates its typed contract."""


class InterpretationChunkSpec(BaseModel):
    """One deterministic chunk in an interpretation bundle."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    chunk_id: str = Field(min_length=1)
    chunk_number: int = Field(ge=0)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: JsonValue


class InterpretationBundleRecord(BaseModel):
    """One source-item version interpreted as a semantic bundle."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    bundle_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    content_version: str = Field(min_length=1)
    interpreter_version: str = Field(min_length=1)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    payload: JsonValue


class InterpretationChunkRecord(InterpretationChunkSpec):
    """Current durable lifecycle of one interpretation chunk."""

    bundle_id: str = Field(min_length=1)
    status: WorkStatus
    claimed_run_id: str | None = None
    claimed_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    output_attempt_ids: tuple[str, ...] = ()
    output: JsonValue = None


class DiscoveryUnitRecord(BaseModel):
    """One compact discovery input that must be evaluated once."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    unit_id: str = Field(min_length=1)
    kind: DiscoveryUnitKind
    subject_id: str = Field(min_length=1)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: str | None = Field(default=None, min_length=1)
    created_at: AwareDatetime
    payload: JsonValue


class DiscoveryOriginRecord(BaseModel):
    """One durable cause of a discovery unit."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    kind: str = Field(min_length=1)
    identifier: str = Field(min_length=1)


class CandidateDiscoveryOriginRecord(BaseModel):
    """Exact discovery unit that produced one candidate thesis."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    candidate_thesis_id: str = Field(min_length=1)
    unit_id: str = Field(min_length=1)


class DiscoveryBatchRecord(BaseModel):
    """One bounded set of discovery units claimed for inference."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    batch_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: DiscoveryBatchStatus
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    unit_ids: tuple[str, ...]
    result_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_candidate_ids: tuple[str, ...] = ()
    validated_output: JsonValue = None


class ResearchJobRecord(BaseModel):
    """Premise-versioned research work reusable across update runs."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    job_id: str = Field(min_length=1)
    parent_job_id: str | None = Field(default=None, min_length=1)
    candidate_thesis_id: str = Field(min_length=1)
    premise_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cycle_number: int = Field(default=0, ge=0)
    review_trigger_at: AwareDatetime | None = None
    source_discovery_unit_id: str | None = Field(default=None, min_length=1)
    created_at: AwareDatetime
    payload: JsonValue


class ClaimedResearchJobRecord(ResearchJobRecord):
    """Current durable counters and lifecycle for a research job."""

    status: ResearchJobStatus
    claimed_run_id: str | None = None
    claimed_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    stop_reason: str | None = None
    wave_count: int = Field(ge=0, le=3)
    search_count: int = Field(ge=0, le=6)
    accepted_fetch_count: int = Field(ge=0, le=12)
    next_review_at: AwareDatetime | None = None


class ResearchCheckpointRecord(BaseModel):
    """Compact cumulative research digest after one focused wave."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    checkpoint_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    wave_number: int = Field(ge=0, le=3)
    search_count: int = Field(ge=0, le=6)
    accepted_fetch_count: int = Field(ge=0, le=12)
    recorded_at: AwareDatetime
    digest: JsonValue


class ResearchWaveIdentity(BaseModel):
    """Stable semantic identity of one candidate research wave."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    job_id: str = Field(min_length=1)
    wave_number: int = Field(ge=1, le=3)

    @property
    def wave_result_id(self) -> str:
        """Return the deterministic durable identifier for this semantic wave."""
        return hashlib.sha256(f"{self.job_id}\0{self.wave_number}".encode()).hexdigest()


class WorkClaim(BaseModel):
    """Run ownership of one recoverable lifecycle transition."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    run_id: str = Field(min_length=1)
    claimed_at: AwareDatetime


class ResearchWaveResultRecord(BaseModel):
    """Validated durable A3 wave result awaiting work-ledger reconciliation."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    wave_result_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    origin_run_id: str = Field(min_length=1)
    execution_completed_run_id: str | None = Field(default=None, min_length=1)
    execution_completed_at: AwareDatetime | None = None
    checkpointed_run_id: str | None = Field(default=None, min_length=1)
    wave_number: int = Field(ge=1, le=3)
    recorded_at: AwareDatetime
    checkpointed_at: AwareDatetime | None = None
    execution: JsonValue
    context: JsonValue
    search_count_delta: int = Field(ge=0)
    accepted_fetch_count_delta: int = Field(ge=0)


class ProviderResearchWaveRecord(BaseModel):
    """Provider-complete A3 wave awaiting any missing interpretations."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    wave_result_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    origin_run_id: str = Field(min_length=1)
    wave_number: int = Field(ge=1, le=3)
    recorded_at: AwareDatetime
    provider_result: JsonValue
    search_count_delta: int = Field(ge=0)
    accepted_fetch_count_delta: int = Field(ge=0)


class IncrementalResearchAdmissionRecord(BaseModel):
    """A3 admission binding prior durable work as inputs without rewriting ownership."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    admission_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    known_at: AwareDatetime
    input_job_ids: tuple[str, ...]
    input_wave_result_ids: tuple[str, ...]
    input_checkpoint_ids: tuple[str, ...]
    output_record_ids: tuple[str, ...]
    payload: JsonValue


class ResearchUriAdmissionRecord(BaseModel):
    """One accepted, rejected, or reused canonical research URI."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    admission_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    canonical_uri: str = Field(min_length=1)
    disposition: UriDisposition
    provenance_group: str | None = Field(default=None, min_length=1)
    consumes_fetch_capacity: bool
    admitted_at: AwareDatetime
    reason: str | None = Field(default=None, min_length=1)
    source_item_id: str | None = Field(default=None, min_length=1)
    asset_id: str | None = Field(default=None, min_length=1)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_version: str | None = Field(default=None, min_length=1)
    source_definition_hash: str | None = Field(default=None, min_length=1)
    processor_name: str | None = Field(default=None, min_length=1)
    processor_version: str | None = Field(default=None, min_length=1)
    interpretation_model: str | None = Field(default=None, min_length=1)
    interpretation_prompt_version: str | None = Field(default=None, min_length=1)
    payload: JsonValue


class SynthesisUnitRecord(BaseModel):
    """Per-candidate synthesis work for one terminal research fingerprint."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    unit_id: str = Field(min_length=1)
    research_job_id: str = Field(min_length=1)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    payload: JsonValue


class ClaimedSynthesisUnitRecord(SynthesisUnitRecord):
    """Current durable lifecycle of one synthesis unit."""

    status: WorkStatus
    claimed_run_id: str | None = None
    claimed_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    checkpoint_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validated_output: JsonValue = None


class SynthesisOutputRecord(BaseModel):
    """Immutable output binding for one completed synthesis unit."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    output_id: str = Field(min_length=1)
    output_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    output_record_ids: tuple[str, ...]
    payload: JsonValue


class RunInferenceUsage(BaseModel):
    """Sanitized aggregate provider usage for one update run."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    run_id: str = Field(min_length=1)
    logical_call_count: int = Field(ge=0)
    failed_call_count: int = Field(ge=0)
    unavailable_usage_call_count: int = Field(ge=0)
    usage: InferenceUsage
    breakdown: tuple[InferenceUsageBreakdown, ...] = ()


class InferenceUsageBreakdown(BaseModel):
    """Provider usage grouped by stage and purpose for one run."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    stage: str = Field(pattern=r"^A[1-4]$")
    purpose: str = Field(min_length=1)
    logical_call_count: int = Field(ge=0)
    unavailable_usage_call_count: int = Field(ge=0)
    usage: InferenceUsage


class FilteredInferenceUsageBreakdown(BaseModel):
    """Usage grouped by exact stage, purpose, and model identity."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    run_id: str = Field(min_length=1)
    stage: str = Field(pattern=r"^A[1-4]$")
    purpose: str = Field(min_length=1)
    model: str = Field(min_length=1)
    logical_call_count: int = Field(ge=0)
    failed_call_count: int = Field(ge=0)
    unavailable_usage_call_count: int = Field(ge=0)
    usage: InferenceUsage


class FilteredInferenceUsage(BaseModel):
    """Provider-free usage diagnostics for an explicit durable query."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    run_id: str | None = None
    since: AwareDatetime | None = None
    stage: str | None = Field(default=None, pattern=r"^A[1-4]$")
    logical_call_count: int = Field(ge=0)
    failed_call_count: int = Field(ge=0)
    unavailable_usage_call_count: int = Field(ge=0)
    completed_unit_count: int = Field(ge=0)
    input_tokens_per_completed_unit: float | None = Field(default=None, ge=0)
    cache_read_fraction: float | None = Field(default=None, ge=0, le=1)
    usage: InferenceUsage
    breakdown: tuple[FilteredInferenceUsageBreakdown, ...]


class IntelligenceWorkStatus(BaseModel):
    """Provider-free queue counts for incremental intelligence."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    source_id: str | None = None
    pending_interpretation_bundles: int = Field(ge=0)
    active_interpretation_bundles: int = Field(ge=0)
    pending_interpretation_chunks: int = Field(ge=0)
    active_interpretation_chunks: int = Field(ge=0)
    pending_discovery_units: int = Field(ge=0)
    active_discovery_units: int = Field(ge=0)
    pending_research_jobs: int = Field(ge=0)
    active_research_jobs: int = Field(ge=0)
    due_research_reviews: int = Field(default=0, ge=0)
    pending_synthesis_units: int = Field(ge=0)
    active_synthesis_units: int = Field(ge=0)
    unmaterialized_interpretation_bundles: int = Field(default=0, ge=0)
    unmaterialized_discovery_units: int = Field(default=0, ge=0)


class IntelligenceWorkCompletionCounts(BaseModel):
    """Exact durable work records completed by one update run."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    interpretation_bundles: int = Field(ge=0)
    interpretation_chunks: int = Field(ge=0)
    discovery_units: int = Field(ge=0)
    research_jobs: int = Field(ge=0)
    synthesis_units: int = Field(ge=0)


class DurableAdvanceMetric(BaseModel):
    """Committed work transitions attributable to one bounded update run."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    run_id: str
    interpretation_chunks: int = Field(ge=0)
    discovery_batches: int = Field(ge=0)
    research_jobs_created: int = Field(ge=0)
    research_wave_results: int = Field(ge=0)
    research_checkpoints: int = Field(ge=0)
    research_tasks_materialized: int = Field(ge=0)
    synthesis_units: int = Field(ge=0)
    total: int = Field(ge=0)


class IntelligenceWorkRepository:
    """Persist and atomically claim incremental intelligence work."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to one initialized database."""
        self._database: Database = database

    def ensure_interpretation_bundle(
        self,
        bundle: InterpretationBundleRecord,
        chunks: tuple[InterpretationChunkSpec, ...],
    ) -> None:
        """Create one immutable bundle and its complete ordered chunk set idempotently."""
        if tuple(chunk.chunk_number for chunk in chunks) != tuple(range(len(chunks))):
            raise WorkTransitionError("Interpretation chunk numbers must be contiguous from zero.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _insert_initial_lifecycle_record(
                connection,
                "interpretation_bundles",
                "bundle_id",
                bundle.bundle_id,
                {
                    "bundle_id": bundle.bundle_id,
                    "source_item_id": bundle.source_item_id,
                    "content_version": bundle.content_version,
                    "interpreter_version": bundle.interpreter_version,
                    "input_fingerprint": bundle.input_fingerprint,
                    "status": WorkStatus.PENDING,
                    "created_at": _time(bundle.created_at),
                    "completed_at": None,
                    "bundle_json": _json(bundle.payload),
                },
                immutable_columns=(
                    "bundle_id",
                    "source_item_id",
                    "content_version",
                    "interpreter_version",
                    "input_fingerprint",
                    "bundle_json",
                ),
            )
            for chunk in chunks:
                _insert_initial_lifecycle_record(
                    connection,
                    "interpretation_bundle_chunks",
                    "chunk_id",
                    chunk.chunk_id,
                    {
                        "chunk_id": chunk.chunk_id,
                        "bundle_id": bundle.bundle_id,
                        "chunk_number": chunk.chunk_number,
                        "input_fingerprint": chunk.input_fingerprint,
                        "status": WorkStatus.PENDING,
                        "claimed_run_id": None,
                        "claimed_at": None,
                        "completed_at": None,
                        "output_attempt_ids_json": "[]",
                        "output_json": "null",
                        "chunk_json": _json(chunk.payload),
                    },
                    immutable_columns=(
                        "chunk_id",
                        "bundle_id",
                        "chunk_number",
                        "input_fingerprint",
                        "chunk_json",
                    ),
                )

    def claim_interpretation_chunk(
        self, *, run_id: str, claimed_at: datetime, reclaim_before: datetime, source_id: str | None = None
    ) -> InterpretationChunkRecord | None:
        """Claim pending, same-run, terminal-owner, or explicitly expired work."""
        _require_reclaim_boundary(claimed_at, reclaim_before)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            parameters: list[object] = [run_id, _time(reclaim_before)]
            source_clause = ""
            if source_id is not None:
                source_clause = "AND item.source_id = ?"
                parameters.append(source_id)
            row = connection.execute(
                f"""SELECT chunk.* FROM interpretation_bundle_chunks AS chunk
                JOIN interpretation_bundles AS bundle USING (bundle_id)
                JOIN source_items AS item ON item.source_item_id = bundle.source_item_id
                    AND item.content_version = bundle.content_version
                WHERE chunk.status != 'completed'
                  AND (chunk.status = 'pending' OR chunk.claimed_run_id = ? OR chunk.claimed_at <= ?
                    OR EXISTS (SELECT 1 FROM run_terminal_events AS terminal
                               WHERE terminal.run_id = chunk.claimed_run_id)) {source_clause}
                ORDER BY CASE chunk.status WHEN 'active' THEN 0 ELSE 1 END,
                         bundle.created_at, bundle.bundle_id, chunk.chunk_number LIMIT 1""",  # noqa: S608  # nosec B608 -- source_clause is selected from fixed internal SQL.
                parameters,
            ).fetchone()
            if row is None:
                return None
            chunk_id = str(row["chunk_id"])
            _ = connection.execute(
                """UPDATE interpretation_bundle_chunks SET status = 'active', claimed_run_id = ?, claimed_at = ?
                WHERE chunk_id = ? AND status != 'completed'""",
                (run_id, _time(claimed_at), chunk_id),
            )
            _ = connection.execute(
                "UPDATE interpretation_bundles SET status = 'active' WHERE bundle_id = ? AND status = 'pending'",
                (str(row["bundle_id"]),),
            )
            return _interpretation_chunk(connection, chunk_id)

    def complete_interpretation_chunk(
        self,
        *,
        chunk_id: str,
        run_id: str,
        completed_at: datetime,
        output_attempt_ids: tuple[str, ...] = (),
        output: JsonValue = None,
    ) -> None:
        """Checkpoint one claimed prompt chunk without exposing its bundle."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """UPDATE interpretation_bundle_chunks SET status = 'completed', completed_at = ?,
                output_attempt_ids_json = ?, output_json = ?
                WHERE chunk_id = ? AND status = 'active' AND claimed_run_id = ?""",
                (_time(completed_at), _json(output_attempt_ids), _json(output), chunk_id, run_id),
            )
            if cursor.rowcount != 1:
                raise WorkTransitionError("Interpretation chunk is not claimed by this run.")

    def interpretation_chunks_for_bundle(self, bundle_id: str) -> tuple[InterpretationChunkRecord, ...]:
        """Return every ordered checkpoint for one bundle."""
        with self._database.read_only_transaction() as connection:
            rows = connection.execute(
                "SELECT chunk_id FROM interpretation_bundle_chunks WHERE bundle_id = ? ORDER BY chunk_number",
                (bundle_id,),
            ).fetchall()
            return tuple(_interpretation_chunk(connection, str(row[0])) for row in rows)

    def ready_interpretation_bundle(self, source_id: str | None = None) -> str | None:
        """Return the oldest fully checkpointed but unadmitted bundle."""
        params: tuple[object, ...] = () if source_id is None else (source_id,)
        source_clause = "" if source_id is None else "AND item.source_id = ?"
        with self._database.read_only_transaction() as connection:
            row = connection.execute(
                f"""SELECT bundle.bundle_id FROM interpretation_bundles AS bundle
                JOIN source_items AS item ON item.source_item_id = bundle.source_item_id
                    AND item.content_version = bundle.content_version
                WHERE bundle.status != 'completed' {source_clause}
                  AND NOT EXISTS (SELECT 1 FROM interpretation_bundle_chunks AS chunk
                    WHERE chunk.bundle_id = bundle.bundle_id AND chunk.status != 'completed')
                ORDER BY bundle.created_at, bundle.bundle_id LIMIT 1""",  # noqa: S608  # nosec B608 -- source_clause is selected from fixed internal SQL.
                params,
            ).fetchone()
        return None if row is None else str(row[0])

    def append_discovery_unit(self, unit: DiscoveryUnitRecord, origins: tuple[DiscoveryOriginRecord, ...]) -> None:
        """Create a compact discovery unit and its durable origins idempotently."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            immutable_values: dict[str, object] = {
                "unit_id": unit.unit_id,
                "unit_kind": unit.kind,
                "subject_id": unit.subject_id,
                "input_fingerprint": unit.input_fingerprint,
                "source_id": unit.source_id,
                "unit_json": _json(unit.payload),
            }
            _ = connection.execute(
                """INSERT OR IGNORE INTO discovery_units
                (unit_id, unit_kind, subject_id, input_fingerprint, source_id, status, created_at, completed_at,
                    unit_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    unit.unit_id,
                    unit.kind,
                    unit.subject_id,
                    unit.input_fingerprint,
                    unit.source_id,
                    WorkStatus.PENDING,
                    _time(unit.created_at),
                    _json(unit.payload),
                ),
            )
            row = connection.execute(
                """SELECT unit_id, unit_kind, subject_id, input_fingerprint, source_id, unit_json
                FROM discovery_units WHERE unit_id = ?""",
                (unit.unit_id,),
            ).fetchone()
            if row is None or tuple(row) != tuple(immutable_values.values()):
                raise ImmutableWorkCollisionError("discovery_units identity is already bound to different content.")
            for origin in origins:
                _ = connection.execute(
                    """INSERT OR IGNORE INTO discovery_unit_origins (unit_id, origin_kind, origin_id)
                    VALUES (?, ?, ?)""",
                    (unit.unit_id, origin.kind, origin.identifier),
                )

    def promote_canonical_claim_change(
        self,
        *,
        canonical_claim_key: str,
        observation_ids: tuple[str, ...],
        input_fingerprint: str,
        promoted_at: datetime,
        operator_action_id: str,
    ) -> str:
        """Explicitly promote one non-recursive claim change into discovery work."""
        unit_id = hashlib.sha256(f"claim-promotion\0{canonical_claim_key}\0{input_fingerprint}".encode()).hexdigest()
        self.append_discovery_unit(
            DiscoveryUnitRecord(
                unit_id=unit_id,
                kind=DiscoveryUnitKind.CANONICAL_CLAIM,
                subject_id=canonical_claim_key,
                input_fingerprint=input_fingerprint,
                source_id=None,
                created_at=promoted_at,
                payload={"observation_ids": list(observation_ids)},
            ),
            (DiscoveryOriginRecord(kind="operator_promotion", identifier=operator_action_id),),
        )
        return unit_id

    def source_id_for_bundle(self, bundle_id: str) -> str:
        """Return the immutable source identity for one interpretation bundle."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT item.source_id FROM interpretation_bundles AS bundle
                JOIN source_items AS item ON item.source_item_id = bundle.source_item_id
                    AND item.content_version = bundle.content_version
                WHERE bundle.bundle_id = ?""",
                (bundle_id,),
            ).fetchone()
        if row is None:
            raise WorkTransitionError("Interpretation bundle has no source item")
        return str(row[0])

    def discovery_units_by_ids(self, unit_ids: tuple[str, ...]) -> tuple[DiscoveryUnitRecord, ...]:
        """Return exact compact discovery inputs in caller order."""
        if not unit_ids:
            return ()
        with self._database.read_only_transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM discovery_units
                WHERE unit_id IN (SELECT value FROM json_each(?))""",
                (_json(unit_ids),),
            ).fetchall()
        by_id = {str(row["unit_id"]): row for row in rows}
        if set(by_id) != set(unit_ids):
            raise WorkTransitionError("Discovery batch contains an unknown unit.")
        try:
            return tuple(
                DiscoveryUnitRecord.model_validate(
                    {
                        "unit_id": unit_id,
                        "kind": by_id[unit_id]["unit_kind"],
                        "subject_id": by_id[unit_id]["subject_id"],
                        "input_fingerprint": by_id[unit_id]["input_fingerprint"],
                        "source_id": by_id[unit_id]["source_id"],
                        "created_at": by_id[unit_id]["created_at"],
                        "payload": json.loads(str(by_id[unit_id]["unit_json"])),
                    }
                )
                for unit_id in unit_ids
            )
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise MalformedWorkRecordError("Stored discovery unit is malformed.") from error

    def discovery_unit(self, unit_id: str) -> DiscoveryUnitRecord | None:
        """Return one discovery unit when materialized."""
        with self._database.read_only_transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM discovery_units WHERE unit_id = ?",
                (unit_id,),
            ).fetchone()
        return None if exists is None else self.discovery_units_by_ids((unit_id,))[0]

    def claim_discovery_batch(
        self,
        *,
        batch_id: str,
        run_id: str,
        created_at: datetime,
        reclaim_before: datetime,
        maximum_units: int,
        source_id: str | None = None,
    ) -> DiscoveryBatchRecord | None:
        """Claim one bounded batch without stealing a live unexpired owner."""
        if maximum_units <= 0:
            raise ValueError("maximum_units must be positive.")
        _require_reclaim_boundary(created_at, reclaim_before)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            active = _active_discovery_batch(connection, run_id, reclaim_before, source_id)
            if active is not None:
                if active.run_id != run_id:
                    _ = connection.execute(
                        "UPDATE discovery_batches SET run_id = ?, created_at = ? WHERE batch_id = ?",
                        (run_id, _time(created_at), active.batch_id),
                    )
                    return _discovery_batch(connection, active.batch_id)
                return active
            params: list[object] = []
            source_clause = ""
            if source_id is not None:
                source_clause = "AND source_id = ?"
                params.append(source_id)
            params.append(maximum_units)
            rows = connection.execute(
                f"""SELECT unit_id FROM discovery_units WHERE status = 'pending' {source_clause}
                ORDER BY created_at, unit_id LIMIT ?""",  # noqa: S608  # nosec B608 -- fixed internal clause.
                params,
            ).fetchall()
            unit_ids = tuple(str(row[0]) for row in rows)
            if not unit_ids:
                return None
            _ = connection.execute(
                """INSERT INTO discovery_batches
                (batch_id, run_id, status, created_at, validated_output_json, batch_json)
                VALUES (?, ?, 'active', ?, 'null', '{}')""",
                (batch_id, run_id, _time(created_at)),
            )
            for unit_id in unit_ids:
                _ = connection.execute("UPDATE discovery_units SET status = 'active' WHERE unit_id = ?", (unit_id,))
                _ = connection.execute(
                    "INSERT INTO discovery_batch_units (batch_id, unit_id) VALUES (?, ?)", (batch_id, unit_id)
                )
            return _discovery_batch(connection, batch_id)

    def complete_discovery_batch(
        self,
        *,
        batch_id: str,
        run_id: str,
        completed_at: datetime,
        result_fingerprint: str,
        output_candidate_ids: tuple[str, ...],
        candidate_origins: tuple[CandidateDiscoveryOriginRecord, ...] = (),
    ) -> None:
        """Complete a batch, including valid empty candidate output."""
        if {origin.candidate_thesis_id for origin in candidate_origins} != set(output_candidate_ids):
            raise WorkTransitionError("Every output candidate requires exact discovery-unit lineage.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            unit_rows = connection.execute(
                "SELECT unit_id FROM discovery_batch_units WHERE batch_id = ?", (batch_id,)
            ).fetchall()
            batch_unit_ids = {str(row[0]) for row in unit_rows}
            if any(origin.unit_id not in batch_unit_ids for origin in candidate_origins):
                raise WorkTransitionError("Candidate origin does not belong to its discovery batch.")
            for origin in candidate_origins:
                _ = connection.execute(
                    """INSERT INTO candidate_discovery_origins
                    (candidate_thesis_id, unit_id, batch_id) VALUES (?, ?, ?)""",
                    (origin.candidate_thesis_id, origin.unit_id, batch_id),
                )
            cursor = connection.execute(
                """UPDATE discovery_batches SET status = 'completed', completed_at = ?,
                result_fingerprint = ?, output_candidate_ids_json = ?
                WHERE batch_id = ? AND status IN ('active', 'checkpointed') AND run_id = ?""",
                (_time(completed_at), result_fingerprint, _json(output_candidate_ids), batch_id, run_id),
            )
            if cursor.rowcount != 1:
                raise WorkTransitionError("Discovery batch is not active.")
            _ = connection.execute(
                """UPDATE discovery_units SET status = 'completed', completed_at = ?
                WHERE unit_id IN (SELECT unit_id FROM discovery_batch_units WHERE batch_id = ?)""",
                (_time(completed_at), batch_id),
            )

    def checkpoint_discovery_output(
        self,
        *,
        batch_id: str,
        run_id: str,
        result_fingerprint: str,
        output_candidate_ids: tuple[str, ...],
        validated_output: JsonValue,
    ) -> None:
        """Persist validated A2 output before fallible domain admission."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """UPDATE discovery_batches SET status = 'checkpointed', result_fingerprint = ?,
                output_candidate_ids_json = ?, validated_output_json = ?
                WHERE batch_id = ? AND run_id = ? AND status = 'active'""",
                (result_fingerprint, _json(output_candidate_ids), _json(validated_output), batch_id, run_id),
            )
            if cursor.rowcount != 1:
                existing = _discovery_batch(connection, batch_id)
                if (
                    existing.run_id != run_id
                    or existing.result_fingerprint != result_fingerprint
                    or existing.output_candidate_ids != output_candidate_ids
                    or _json(existing.validated_output) != _json(validated_output)
                ):
                    raise WorkTransitionError("Discovery output checkpoint conflicts with durable state.")

    def discovery_origin_unit_ids(
        self,
        candidate_thesis_id: str,
        source_id: str | None = None,
    ) -> tuple[str, ...]:
        """Return candidate lineage, restricted to the requested source when supplied."""
        with self._database.transaction() as connection:
            if source_id is None:
                rows = connection.execute(
                    """SELECT unit_id FROM candidate_discovery_origins
                    WHERE candidate_thesis_id = ? ORDER BY unit_id""",
                    (candidate_thesis_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT origin.unit_id FROM candidate_discovery_origins AS origin
                    JOIN discovery_units AS unit ON unit.unit_id = origin.unit_id
                    WHERE origin.candidate_thesis_id = ? AND unit.source_id = ?
                    ORDER BY origin.unit_id""",
                    (candidate_thesis_id, source_id),
                ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def candidates_for_research_reconciliation(self, source_id: str | None = None) -> tuple[str, ...]:
        """Return every durable A2 candidate whose current premise must be reconciled."""
        params: tuple[object, ...] = () if source_id is None else (source_id,)
        source_clause = "" if source_id is None else "AND unit.source_id = ?"
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT origin.candidate_thesis_id
                FROM candidate_discovery_origins AS origin
                JOIN discovery_units AS unit ON unit.unit_id = origin.unit_id
                WHERE 1 = 1 {source_clause}
                ORDER BY origin.candidate_thesis_id""",  # noqa: S608  # nosec B608 -- source_clause is selected from fixed internal SQL.
                params,
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def candidates_without_research_jobs(self, source_id: str | None = None) -> tuple[str, ...]:
        """Compatibility name for all candidates requiring premise reconciliation."""
        return self.candidates_for_research_reconciliation(source_id)

    def latest_research_job_for_candidate(
        self,
        candidate_thesis_id: str,
        source_id: str | None = None,
    ) -> ClaimedResearchJobRecord | None:
        """Return the latest research cycle for a candidate's exact source scope."""
        with self._database.transaction() as connection:
            if source_id is None:
                row = connection.execute(
                    """SELECT job_id FROM research_jobs WHERE candidate_thesis_id = ?
                    ORDER BY cycle_number DESC, created_at DESC, job_id DESC LIMIT 1""",
                    (candidate_thesis_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT job.job_id FROM research_jobs AS job
                    WHERE job.candidate_thesis_id = ?
                      AND EXISTS (
                        SELECT 1 FROM research_job_discovery_origins AS origin
                        JOIN discovery_units AS unit ON unit.unit_id = origin.unit_id
                        WHERE origin.job_id = job.job_id AND unit.source_id = ?
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM research_job_discovery_origins AS origin
                        JOIN discovery_units AS unit ON unit.unit_id = origin.unit_id
                        WHERE origin.job_id = job.job_id
                          AND (unit.source_id IS NULL OR unit.source_id != ?)
                      )
                    ORDER BY job.cycle_number DESC, job.created_at DESC, job.job_id DESC LIMIT 1""",
                    (candidate_thesis_id, source_id, source_id),
                ).fetchone()
            return None if row is None else _research_job(connection, str(row[0]))

    def ensure_research_job(self, job: ResearchJobRecord, origin_unit_ids: tuple[str, ...] = ()) -> None:
        """Create one premise-versioned research job idempotently."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            existing = connection.execute(
                """SELECT parent_job_id, candidate_thesis_id, premise_fingerprint, cycle_number,
                review_trigger_at, source_discovery_unit_id, created_at, job_json
                FROM research_jobs WHERE job_id = ?""",
                (job.job_id,),
            ).fetchone()
            semantic_identity = (
                job.parent_job_id,
                job.candidate_thesis_id,
                job.premise_fingerprint,
                job.cycle_number,
                _optional_time(job.review_trigger_at),
                job.source_discovery_unit_id,
                _time(job.created_at),
                _json(job.payload),
            )
            if existing is None:
                _ = connection.execute(
                    """INSERT INTO research_jobs
                    (job_id, parent_job_id, candidate_thesis_id, premise_fingerprint, cycle_number,
                     review_trigger_at, source_discovery_unit_id, status, created_at, claimed_run_id,
                     claimed_at, completed_at, stop_reason, wave_count, search_count,
                     accepted_fetch_count, next_review_at, job_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, NULL, 0, 0, 0, NULL, ?)""",
                    (job.job_id, *semantic_identity),
                )
                for unit_id in origin_unit_ids:
                    _ = connection.execute(
                        """INSERT INTO research_job_discovery_origins (job_id, unit_id) VALUES (?, ?)""",
                        (job.job_id, unit_id),
                    )
            else:
                if tuple(existing) != semantic_identity:
                    raise ImmutableWorkCollisionError("Research job identity is bound to different premises.")
                stored_origins = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """SELECT unit_id FROM research_job_discovery_origins
                        WHERE job_id = ? ORDER BY unit_id""",
                        (job.job_id,),
                    ).fetchall()
                )
                if stored_origins != tuple(sorted(origin_unit_ids)):
                    raise ImmutableWorkCollisionError("Research job identity is bound to different source origins.")

    def claim_research_jobs(
        self,
        *,
        run_id: str,
        claimed_at: datetime,
        reclaim_before: datetime,
        maximum_jobs: int = 2,
        source_id: str | None = None,
    ) -> tuple[ClaimedResearchJobRecord, ...]:
        """Claim resumable, due-review, then pending jobs without stealing live leases."""
        if maximum_jobs <= 0:
            raise ValueError("maximum_jobs must be positive.")
        _require_reclaim_boundary(claimed_at, reclaim_before)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            params: list[object] = [run_id, _time(reclaim_before), _time(claimed_at)]
            source_clause = ""
            if source_id is not None:
                source_clause = """AND EXISTS (
                    SELECT 1 FROM research_job_discovery_origins AS origin
                    JOIN discovery_units AS unit USING (unit_id)
                    WHERE origin.job_id = job.job_id AND unit.source_id = ?)
                    AND NOT EXISTS (
                    SELECT 1 FROM research_job_discovery_origins AS origin
                    JOIN discovery_units AS unit USING (unit_id)
                    WHERE origin.job_id = job.job_id
                      AND (unit.source_id IS NULL OR unit.source_id != ?))"""
                params.append(source_id)
                params.append(source_id)
            params.append(maximum_jobs)
            rows = connection.execute(
                f"""SELECT job_id, status, next_review_at FROM research_jobs AS job WHERE (
                  (status != 'terminal' AND (
                    status = 'pending' OR claimed_run_id = ? OR claimed_at <= ?
                    OR EXISTS (SELECT 1 FROM run_terminal_events AS terminal
                               WHERE terminal.run_id = job.claimed_run_id)))
                  OR (status = 'terminal' AND next_review_at IS NOT NULL AND next_review_at <= ?)
                ) {source_clause}
                ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'terminal' THEN 1 ELSE 2 END,
                         created_at, job_id LIMIT ?""",  # noqa: S608  # nosec B608 -- fixed internal clause.
                params,
            ).fetchall()
            claimed_job_ids: list[str] = []
            for row in rows:
                job_id = str(row[0])
                if str(row[1]) == ResearchJobStatus.TERMINAL:
                    trigger_at = str(row[2])
                    successor_id = _review_cycle_job_id(job_id, trigger_at)
                    _create_review_cycle(
                        connection,
                        parent_job_id=job_id,
                        successor_job_id=successor_id,
                        review_trigger_at=trigger_at,
                        run_id=run_id,
                        claimed_at=claimed_at,
                    )
                    claimed_job_ids.append(successor_id)
                else:
                    _ = connection.execute(
                        """UPDATE research_jobs SET status = 'active', claimed_run_id = ?, claimed_at = ?
                        WHERE job_id = ? AND status != 'terminal'""",
                        (run_id, _time(claimed_at), job_id),
                    )
                    claimed_job_ids.append(job_id)
            job_ids = tuple(claimed_job_ids)
            return tuple(_research_job(connection, job_id) for job_id in job_ids)

    def uncheckpointed_wave(self, job_id: str) -> ResearchWaveResultRecord | None:
        """Return the earliest durable A3 wave not reconciled to the work ledger."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM research_wave_results
                WHERE job_id = ? AND phase = 'execution_completed' AND checkpointed_at IS NULL
                ORDER BY wave_number LIMIT 1""",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        envelope = json.loads(str(row["execution_json"]))
        if not isinstance(envelope, dict):
            raise MalformedWorkRecordError("Stored research wave envelope is malformed.")
        return ResearchWaveResultRecord.model_validate(
            {
                "wave_result_id": row["wave_result_id"],
                "job_id": row["job_id"],
                "session_id": row["session_id"],
                "origin_run_id": row["run_id"],
                "execution_completed_run_id": row["execution_completed_run_id"],
                "execution_completed_at": row["execution_completed_at"],
                "checkpointed_run_id": row["checkpointed_run_id"],
                "wave_number": row["wave_number"],
                "recorded_at": row["recorded_at"],
                "checkpointed_at": row["checkpointed_at"],
                **envelope,
            }
        )

    def record_provider_wave(self, record: ProviderResearchWaveRecord) -> None:
        """Create one immutable provider-completed research wave."""
        envelope = {
            "search_count_delta": record.search_count_delta,
            "accepted_fetch_count_delta": record.accepted_fetch_count_delta,
        }
        with self._database.transaction(TransactionMode.WRITE) as connection:
            existing = connection.execute(
                "SELECT * FROM research_wave_results WHERE wave_result_id = ?",
                (record.wave_result_id,),
            ).fetchone()
            immutable = (
                record.job_id,
                record.session_id,
                record.origin_run_id,
                record.wave_number,
                _time(record.recorded_at),
                _json(record.provider_result),
            )
            if existing is None:
                _ = connection.execute(
                    """INSERT INTO research_wave_results
                    (wave_result_id, job_id, session_id, run_id, wave_number, phase, recorded_at,
                     checkpointed_at, provider_result_json, execution_json)
                    VALUES (?, ?, ?, ?, ?, 'provider_completed', ?, NULL, ?, ?)""",
                    (record.wave_result_id, *immutable, _json(envelope)),
                )
                return
            stored = (
                str(existing["job_id"]),
                str(existing["session_id"]),
                str(existing["run_id"]),
                int(existing["wave_number"]),
                str(existing["recorded_at"]),
                str(existing["provider_result_json"]),
            )
            if stored != immutable:
                raise WorkTransitionError("Research provider wave identity conflicts with durable state.")

    def provider_wave_deltas(self, *, job_id: str, session_id: str) -> tuple[int, int]:
        """Return exact query and accepted-fetch deltas for one provider wave."""
        with self._database.read_only_transaction() as connection:
            session = connection.execute(
                "SELECT query_count FROM research_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            checkpoint = connection.execute(
                """SELECT accepted_fetch_count FROM research_job_checkpoints
                WHERE job_id = ? ORDER BY wave_number DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
            admitted = connection.execute(
                """SELECT count(*) FROM research_uri_admissions
                WHERE job_id = ? AND consumes_fetch_capacity = 1""",
                (job_id,),
            ).fetchone()
        if session is None or admitted is None:
            raise WorkTransitionError("Research provider wave lacks durable budget state.")
        accepted = int(admitted[0])
        prior = 0 if checkpoint is None else int(checkpoint[0])
        if accepted < prior:
            raise WorkTransitionError("Accepted fetch count precedes its durable checkpoint.")
        return int(session[0]), accepted - prior

    def pending_provider_wave(self, job_id: str) -> ProviderResearchWaveRecord | None:
        """Return the earliest provider-complete wave awaiting execution assembly."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM research_wave_results
                WHERE job_id = ? AND phase = 'provider_completed'
                ORDER BY wave_number LIMIT 1""",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        counters = json.loads(str(row["execution_json"]))
        if not isinstance(counters, dict):
            raise MalformedWorkRecordError("Stored provider wave counters are malformed.")
        return ProviderResearchWaveRecord.model_validate(
            {
                "wave_result_id": row["wave_result_id"],
                "job_id": row["job_id"],
                "session_id": row["session_id"],
                "origin_run_id": row["run_id"],
                "wave_number": row["wave_number"],
                "recorded_at": row["recorded_at"],
                "provider_result": json.loads(str(row["provider_result_json"])),
                **counters,
            }
        )

    def complete_provider_wave(
        self,
        *,
        wave_result_id: str,
        completion_run_id: str,
        execution: JsonValue,
        context: JsonValue,
        completed_at: datetime,
    ) -> None:
        """Add semantic interpretation output to an existing provider wave."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            row = connection.execute(
                """SELECT phase, execution_json, execution_completed_run_id, execution_completed_at
                FROM research_wave_results WHERE wave_result_id = ?""",
                (wave_result_id,),
            ).fetchone()
            if row is None:
                raise WorkTransitionError("Research execution has no provider-wave predecessor.")
            counters = json.loads(str(row["execution_json"]))
            completed = _json({**counters, "execution": execution, "context": context})
            if str(row["phase"]) == "provider_completed":
                _ = connection.execute(
                    """UPDATE research_wave_results SET phase = 'execution_completed', execution_json = ?,
                    execution_completed_run_id = ?, execution_completed_at = ?
                    WHERE wave_result_id = ? AND phase = 'provider_completed'""",
                    (completed, completion_run_id, _time(completed_at), wave_result_id),
                )
            elif (
                str(row["phase"]) != "execution_completed"
                or str(row["execution_json"]) != completed
                or str(row["execution_completed_run_id"]) != completion_run_id
                or str(row["execution_completed_at"]) != _time(completed_at)
            ):
                raise WorkTransitionError("Research wave execution completion conflicts with durable state.")

    def checkpoint_completed_wave(
        self,
        *,
        wave_result_id: str,
        checkpoint_run_id: str,
        digest: JsonValue,
        recorded_at: datetime,
    ) -> ResearchCheckpointRecord:
        """Atomically checkpoint one execution-completed wave from stored deltas."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            wave = connection.execute(
                """SELECT job_id, wave_number, phase, checkpointed_at, checkpointed_run_id,
                execution_json FROM research_wave_results
                WHERE wave_result_id = ?""",
                (wave_result_id,),
            ).fetchone()
            if wave is None or str(wave["phase"]) != "execution_completed":
                raise WorkTransitionError("Research checkpoint requires an execution-completed wave.")
            job_id = str(wave["job_id"])
            wave_number = int(wave["wave_number"])
            envelope_value = json.loads(str(wave["execution_json"]))
            if not isinstance(envelope_value, dict):
                raise MalformedWorkRecordError("Stored research wave envelope is malformed.")
            envelope = cast("dict[str, object]", envelope_value)
            search_value = envelope.get("search_count_delta")
            fetch_value = envelope.get("accepted_fetch_count_delta")
            if not isinstance(search_value, int) or not isinstance(fetch_value, int):
                raise MalformedWorkRecordError("Stored research wave counters are malformed.")
            search_delta = search_value
            fetch_delta = fetch_value
            prior = connection.execute(
                """SELECT search_count, accepted_fetch_count FROM research_job_checkpoints
                WHERE job_id = ? ORDER BY wave_number DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
            search_count = (0 if prior is None else int(prior[0])) + search_delta
            accepted_fetch_count = (0 if prior is None else int(prior[1])) + fetch_delta
            checkpoint_id = (
                "research-checkpoint:"
                + hashlib.sha256(_json({"job_id": job_id, "wave_number": wave_number}).encode()).hexdigest()
            )
            checkpoint = ResearchCheckpointRecord(
                checkpoint_id=checkpoint_id,
                job_id=job_id,
                run_id=checkpoint_run_id,
                wave_number=wave_number,
                search_count=search_count,
                accepted_fetch_count=accepted_fetch_count,
                recorded_at=recorded_at,
                digest=digest,
            )
            _insert_or_require(
                connection,
                "research_job_checkpoints",
                "checkpoint_id",
                checkpoint.checkpoint_id,
                {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "job_id": checkpoint.job_id,
                    "run_id": checkpoint.run_id,
                    "wave_number": checkpoint.wave_number,
                    "search_count": checkpoint.search_count,
                    "accepted_fetch_count": checkpoint.accepted_fetch_count,
                    "recorded_at": _time(checkpoint.recorded_at),
                    "digest_json": _json(checkpoint.digest),
                },
            )
            cursor = connection.execute(
                """UPDATE research_jobs SET wave_count = ?, search_count = ?, accepted_fetch_count = ?
                WHERE job_id = ? AND status = 'active' AND wave_count <= ?
                  AND search_count <= ? AND accepted_fetch_count <= ?""",
                (
                    checkpoint.wave_number,
                    checkpoint.search_count,
                    checkpoint.accepted_fetch_count,
                    checkpoint.job_id,
                    checkpoint.wave_number,
                    checkpoint.search_count,
                    checkpoint.accepted_fetch_count,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkTransitionError("Research checkpoint does not advance an active job monotonically.")
            checkpointed_text = _time(recorded_at)
            if wave["checkpointed_at"] is None:
                _ = connection.execute(
                    """UPDATE research_wave_results SET checkpointed_at = ?, checkpointed_run_id = ?
                    WHERE wave_result_id = ?""",
                    (checkpointed_text, checkpoint_run_id, wave_result_id),
                )
            elif (
                str(wave["checkpointed_at"]) != checkpointed_text
                or str(wave["checkpointed_run_id"]) != checkpoint_run_id
            ):
                raise WorkTransitionError("Research wave was reconciled at a different boundary.")
            return checkpoint

    def admit_incremental_research(self, admission: IncrementalResearchAdmissionRecord) -> None:
        """Bind an A3 artifact to durable cross-run work without changing child ownership."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            artifact = connection.execute(
                "SELECT run_id, stage, known_at FROM stage_artifacts WHERE artifact_id = ?",
                (admission.artifact_id,),
            ).fetchone()
            if artifact is None or tuple(artifact) != (admission.run_id, "A3", _time(admission.known_at)):
                raise WorkTransitionError("Incremental research artifact binding is inconsistent.")
            _require_exact_ids(connection, "research_jobs", "job_id", admission.input_job_ids)
            _require_exact_ids(connection, "research_wave_results", "wave_result_id", admission.input_wave_result_ids)
            _require_exact_ids(connection, "research_job_checkpoints", "checkpoint_id", admission.input_checkpoint_ids)
            _insert_or_require(
                connection,
                "incremental_research_admissions",
                "admission_id",
                admission.admission_id,
                {
                    "admission_id": admission.admission_id,
                    "run_id": admission.run_id,
                    "artifact_id": admission.artifact_id,
                    "known_at": _time(admission.known_at),
                    "input_job_ids_json": _json(admission.input_job_ids),
                    "input_wave_result_ids_json": _json(admission.input_wave_result_ids),
                    "input_checkpoint_ids_json": _json(admission.input_checkpoint_ids),
                    "output_record_ids_json": _json(admission.output_record_ids),
                    "admission_json": _json(admission.payload),
                },
            )

    def latest_research_checkpoint(self, job_id: str) -> ResearchCheckpointRecord | None:
        """Return the latest durable cumulative checkpoint for one job."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM research_job_checkpoints WHERE job_id = ?
                ORDER BY wave_number DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return ResearchCheckpointRecord(
            checkpoint_id=str(row["checkpoint_id"]),
            job_id=str(row["job_id"]),
            run_id=row["run_id"],
            wave_number=int(row["wave_number"]),
            search_count=int(row["search_count"]),
            accepted_fetch_count=int(row["accepted_fetch_count"]),
            recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
            digest=json.loads(str(row["digest_json"])),
        )

    def append_uri_admission(self, admission: ResearchUriAdmissionRecord) -> None:
        """Append a URI decision without charging rejected or reused URIs as fetches."""
        if admission.disposition is not UriDisposition.ACCEPTED and admission.consumes_fetch_capacity:
            raise WorkTransitionError("Only accepted URIs may consume fetch capacity.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _insert_or_require(
                connection,
                "research_uri_admissions",
                "admission_id",
                admission.admission_id,
                {
                    "admission_id": admission.admission_id,
                    "job_id": admission.job_id,
                    "canonical_uri": admission.canonical_uri,
                    "disposition": admission.disposition,
                    "provenance_group": admission.provenance_group,
                    "consumes_fetch_capacity": int(admission.consumes_fetch_capacity),
                    "admitted_at": _time(admission.admitted_at),
                    "reason": admission.reason,
                    "source_item_id": admission.source_item_id,
                    "asset_id": admission.asset_id,
                    "content_hash": admission.content_hash,
                    "content_version": admission.content_version,
                    "source_definition_hash": admission.source_definition_hash,
                    "processor_name": admission.processor_name,
                    "processor_version": admission.processor_version,
                    "interpretation_model": admission.interpretation_model,
                    "interpretation_prompt_version": admission.interpretation_prompt_version,
                    "admission_json": _json(admission.payload),
                },
            )

    def reusable_uri_admission(
        self,
        canonical_uri: str,
        *,
        content_hash: str,
        content_version: str,
        source_definition_hash: str,
        processor_name: str,
        processor_version: str,
        interpretation_model: str,
        interpretation_prompt_version: str,
    ) -> ResearchUriAdmissionRecord | None:
        """Return an asset admitted under the exact reusable processing identity."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM research_uri_admissions
                WHERE canonical_uri = ? AND disposition IN ('accepted', 'reused')
                  AND asset_id IS NOT NULL
                  AND content_hash = ? AND content_version = ? AND source_definition_hash = ?
                  AND processor_name = ? AND processor_version = ?
                  AND interpretation_model = ? AND interpretation_prompt_version = ?
                ORDER BY admitted_at DESC, admission_id DESC LIMIT 1""",
                (
                    canonical_uri,
                    content_hash,
                    content_version,
                    source_definition_hash,
                    processor_name,
                    processor_version,
                    interpretation_model,
                    interpretation_prompt_version,
                ),
            ).fetchone()
        if row is None:
            return None
        try:
            return ResearchUriAdmissionRecord.model_validate(
                {
                    "admission_id": row["admission_id"],
                    "job_id": row["job_id"],
                    "canonical_uri": row["canonical_uri"],
                    "disposition": row["disposition"],
                    "provenance_group": row["provenance_group"],
                    "consumes_fetch_capacity": bool(row["consumes_fetch_capacity"]),
                    "admitted_at": row["admitted_at"],
                    "reason": row["reason"],
                    "source_item_id": row["source_item_id"],
                    "asset_id": row["asset_id"],
                    "content_hash": row["content_hash"],
                    "content_version": row["content_version"],
                    "source_definition_hash": row["source_definition_hash"],
                    "processor_name": row["processor_name"],
                    "processor_version": row["processor_version"],
                    "interpretation_model": row["interpretation_model"],
                    "interpretation_prompt_version": row["interpretation_prompt_version"],
                    "payload": json.loads(str(row["admission_json"])),
                }
            )
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise MalformedWorkRecordError("Stored URI admission is malformed.") from error

    def uri_admission_for_job(
        self,
        job_id: str,
        canonical_uri: str,
        *,
        content_hash: str | None = None,
        content_version: str | None = None,
        source_definition_hash: str | None = None,
        processor_name: str | None = None,
        processor_version: str | None = None,
        interpretation_model: str | None = None,
        interpretation_prompt_version: str | None = None,
    ) -> ResearchUriAdmissionRecord | None:
        """Return an existing decision for one job and exact reusable identity."""
        with self._database.transaction() as connection:
            identity = (
                content_hash,
                content_version,
                source_definition_hash,
                processor_name,
                processor_version,
                interpretation_model,
                interpretation_prompt_version,
            )
            if any(value is None for value in identity) and not all(value is None for value in identity):
                raise ValueError("URI processing identity must be omitted or supply all seven fields.")
            if all(value is None for value in identity):
                row = connection.execute(
                    """SELECT * FROM research_uri_admissions
                    WHERE job_id = ? AND canonical_uri = ?
                    ORDER BY admitted_at DESC, admission_id DESC LIMIT 1""",
                    (job_id, canonical_uri),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT * FROM research_uri_admissions WHERE job_id = ? AND canonical_uri = ?
                    AND content_hash IS ? AND content_version IS ? AND source_definition_hash IS ? AND processor_name IS ?
                    AND processor_version IS ? AND interpretation_model IS ?
                    AND interpretation_prompt_version IS ?
                    ORDER BY admitted_at DESC, admission_id DESC LIMIT 1""",
                    (job_id, canonical_uri, *identity),
                ).fetchone()
        if row is None:
            return None
        try:
            return ResearchUriAdmissionRecord.model_validate(
                {
                    "admission_id": row["admission_id"],
                    "job_id": row["job_id"],
                    "canonical_uri": row["canonical_uri"],
                    "disposition": row["disposition"],
                    "provenance_group": row["provenance_group"],
                    "consumes_fetch_capacity": bool(row["consumes_fetch_capacity"]),
                    "admitted_at": row["admitted_at"],
                    "reason": row["reason"],
                    "source_item_id": row["source_item_id"],
                    "asset_id": row["asset_id"],
                    "content_hash": row["content_hash"],
                    "content_version": row["content_version"],
                    "source_definition_hash": row["source_definition_hash"],
                    "processor_name": row["processor_name"],
                    "processor_version": row["processor_version"],
                    "interpretation_model": row["interpretation_model"],
                    "interpretation_prompt_version": row["interpretation_prompt_version"],
                    "payload": json.loads(str(row["admission_json"])),
                }
            )
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise MalformedWorkRecordError("Stored URI admission is malformed.") from error

    def finalize_research_job(
        self, *, job_id: str, completed_at: datetime, stop_reason: str, next_review_at: datetime | None = None
    ) -> None:
        """Mark active research terminal until material inputs change or review is due."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """UPDATE research_jobs SET status = 'terminal', completed_at = ?, stop_reason = ?,
                next_review_at = ? WHERE job_id = ? AND status = 'active'""",
                (_time(completed_at), stop_reason, _optional_time(next_review_at), job_id),
            )
            if cursor.rowcount != 1:
                raise WorkTransitionError("Research job is not active.")

    def ensure_synthesis_unit(self, unit: SynthesisUnitRecord) -> None:
        """Create one per-candidate synthesis unit idempotently."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _insert_initial_lifecycle_record(
                connection,
                "synthesis_units",
                "unit_id",
                unit.unit_id,
                {
                    "unit_id": unit.unit_id,
                    "research_job_id": unit.research_job_id,
                    "input_fingerprint": unit.input_fingerprint,
                    "status": WorkStatus.PENDING,
                    "created_at": _time(unit.created_at),
                    "claimed_run_id": None,
                    "claimed_at": None,
                    "completed_at": None,
                    "checkpoint_fingerprint": None,
                    "validated_output_json": "null",
                    "unit_payload_hash": hashlib.sha256(_json(unit.payload).encode()).hexdigest(),
                    "unit_json": _json(unit.payload),
                },
                immutable_columns=(
                    "unit_id",
                    "research_job_id",
                    "input_fingerprint",
                    "unit_payload_hash",
                    "unit_json",
                ),
            )

    def claim_synthesis_unit(
        self, *, run_id: str, claimed_at: datetime, reclaim_before: datetime, source_id: str | None = None
    ) -> ClaimedSynthesisUnitRecord | None:
        """Claim the next synthesis unit, reclaiming interrupted work first."""
        _require_reclaim_boundary(claimed_at, reclaim_before)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            params: list[object] = [run_id, _time(reclaim_before)]
            source_clause = ""
            if source_id is not None:
                source_clause = """AND EXISTS (
                SELECT 1 FROM research_job_discovery_origins AS origin
                JOIN discovery_units AS discovery USING (unit_id)
                WHERE origin.job_id = job.job_id AND discovery.source_id = ?)
                AND NOT EXISTS (
                SELECT 1 FROM research_job_discovery_origins AS origin
                JOIN discovery_units AS discovery USING (unit_id)
                WHERE origin.job_id = job.job_id
                  AND (discovery.source_id IS NULL OR discovery.source_id != ?))"""
                params.append(source_id)
                params.append(source_id)
            row = connection.execute(
                f"""SELECT unit.unit_id FROM synthesis_units AS unit
                JOIN research_jobs AS job ON job.job_id = unit.research_job_id
                WHERE unit.status != 'completed'
                  AND (unit.status = 'pending' OR unit.claimed_run_id = ? OR unit.claimed_at <= ?
                    OR EXISTS (SELECT 1 FROM run_terminal_events AS terminal
                               WHERE terminal.run_id = unit.claimed_run_id)) {source_clause}
                ORDER BY CASE unit.status WHEN 'active' THEN 0 ELSE 1 END,
                         unit.created_at, unit.unit_id LIMIT 1""",  # noqa: S608  # nosec B608 -- fixed internal clause.
                params,
            ).fetchone()
            if row is None:
                return None
            unit_id = str(row[0])
            _ = connection.execute(
                """UPDATE synthesis_units
                SET status = CASE WHEN status = 'checkpointed' THEN 'checkpointed' ELSE 'active' END,
                    claimed_run_id = ?, claimed_at = ?
                WHERE unit_id = ? AND status != 'completed'""",
                (run_id, _time(claimed_at), unit_id),
            )
            return _synthesis_unit(connection, unit_id)

    def checkpoint_synthesis_output(
        self, *, unit_id: str, run_id: str, output_fingerprint: str, validated_output: JsonValue
    ) -> None:
        """Persist validated A4 output before fallible domain admission."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """UPDATE synthesis_units SET status = 'checkpointed', checkpoint_fingerprint = ?,
                validated_output_json = ? WHERE unit_id = ? AND claimed_run_id = ? AND status = 'active'""",
                (output_fingerprint, _json(validated_output), unit_id, run_id),
            )
            if cursor.rowcount != 1:
                existing = _synthesis_unit(connection, unit_id)
                if (
                    existing.claimed_run_id != run_id
                    or existing.checkpoint_fingerprint != output_fingerprint
                    or _json(existing.validated_output) != _json(validated_output)
                ):
                    raise WorkTransitionError("Synthesis output checkpoint conflicts with durable state.")

    def complete_synthesis_unit(
        self, *, unit_id: str, run_id: str, completed_at: datetime, output: SynthesisOutputRecord
    ) -> None:
        """Atomically bind an immutable output and complete one claimed synthesis unit."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            row = connection.execute(
                """SELECT status, claimed_run_id, unit_payload_hash, unit_json
                FROM synthesis_units WHERE unit_id = ?""",
                (unit_id,),
            ).fetchone()
            if row is None or str(row[0]) not in {"active", "checkpointed"} or str(row[1]) != run_id:
                raise WorkTransitionError("Synthesis unit is not claimed by this run.")
            if hashlib.sha256(str(row[3]).encode()).hexdigest() != str(row[2]):
                raise WorkTransitionError("Synthesis unit semantic context changed after admission.")
            _insert_or_require(
                connection,
                "synthesis_outputs",
                "output_id",
                output.output_id,
                {
                    "output_id": output.output_id,
                    "unit_id": unit_id,
                    "output_fingerprint": output.output_fingerprint,
                    "created_at": _time(output.created_at),
                    "output_record_ids_json": _json(output.output_record_ids),
                    "output_json": _json(output.payload),
                },
            )
            _ = connection.execute(
                "UPDATE synthesis_units SET status = 'completed', completed_at = ? WHERE unit_id = ?",
                (_time(completed_at), unit_id),
            )

    def record_inference_call(self, record: InferenceCallRecord) -> None:
        """Persist one sanitized completed inference record; prompts are never accepted."""
        call_id = hashlib.sha256(
            "\x1f".join(
                (
                    record.correlation.run_id,
                    record.correlation.work_unit_id,
                    record.stage,
                    record.purpose,
                    record.request_hash,
                    record.started_at.isoformat(),
                )
            ).encode()
        ).hexdigest()
        failure_kind = record.failure_kind if record.status == "failed" else None
        usage = record.usage
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _insert_or_require(
                connection,
                "inference_calls",
                "call_id",
                call_id,
                {
                    "call_id": call_id,
                    "run_id": record.correlation.run_id,
                    "work_unit_id": record.correlation.work_unit_id,
                    "stage": record.stage,
                    "purpose": record.purpose,
                    "model": record.model,
                    "request_hash": record.request_hash,
                    "status": record.status,
                    "started_at": _time(record.started_at),
                    "completed_at": _time(record.completed_at),
                    "usage_available": int(usage is not None),
                    "input_tokens": None if usage is None else usage.input_tokens,
                    "cache_read_tokens": None if usage is None else usage.cache_read_tokens,
                    "cache_write_tokens": None if usage is None else usage.cache_write_tokens,
                    "output_tokens": None if usage is None else usage.output_tokens,
                    "request_count": None if usage is None else usage.request_count,
                    "elapsed_milliseconds": record.elapsed_milliseconds,
                    "failure_kind": failure_kind,
                },
            )

    def usage_for_run(self, run_id: str) -> RunInferenceUsage:
        """Return provider-reported token totals without reading prompts or providers."""
        with self._database.read_only_transaction() as connection:
            row = connection.execute(
                """SELECT count(*), sum(status = 'failed'), sum(usage_available = 0),
                coalesce(sum(input_tokens), 0),
                coalesce(sum(cache_write_tokens), 0), coalesce(sum(cache_read_tokens), 0),
                coalesce(sum(output_tokens), 0), coalesce(sum(request_count), 0)
                FROM inference_calls WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            breakdown_rows = connection.execute(
                """SELECT stage, purpose, count(*), sum(usage_available = 0),
                coalesce(sum(input_tokens), 0), coalesce(sum(cache_write_tokens), 0),
                coalesce(sum(cache_read_tokens), 0), coalesce(sum(output_tokens), 0),
                coalesce(sum(request_count), 0)
                FROM inference_calls WHERE run_id = ? GROUP BY stage, purpose ORDER BY stage, purpose""",
                (run_id,),
            ).fetchall()
        return RunInferenceUsage(
            run_id=run_id,
            logical_call_count=int(row[0]),
            failed_call_count=int(row[1] or 0),
            unavailable_usage_call_count=int(row[2] or 0),
            usage=InferenceUsage(
                input_tokens=int(row[3]),
                cache_write_tokens=int(row[4]),
                cache_read_tokens=int(row[5]),
                output_tokens=int(row[6]),
                request_count=int(row[7]),
            ),
            breakdown=tuple(
                InferenceUsageBreakdown(
                    stage=str(item[0]),
                    purpose=str(item[1]),
                    logical_call_count=int(item[2]),
                    unavailable_usage_call_count=int(item[3] or 0),
                    usage=InferenceUsage(
                        input_tokens=int(item[4]),
                        cache_write_tokens=int(item[5]),
                        cache_read_tokens=int(item[6]),
                        output_tokens=int(item[7]),
                        request_count=int(item[8]),
                    ),
                )
                for item in breakdown_rows
            ),
        )

    def filtered_usage(
        self,
        *,
        run_id: str | None = None,
        since: datetime | None = None,
        stage: str | None = None,
    ) -> FilteredInferenceUsage:
        """Aggregate sanitized inference usage for explicit optional filters."""
        if since is not None and since.tzinfo is None:
            raise ValueError("usage boundary must be timezone-aware")
        if stage is not None and stage not in {"A1", "A2", "A3", "A4"}:
            raise ValueError("usage stage must be A1 through A4")
        clauses: list[str] = []
        params: list[object] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(_time(since))
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        with self._database.read_only_transaction() as connection:
            row = connection.execute(
                f"""SELECT count(*), sum(status = 'failed'), sum(usage_available = 0),
                coalesce(sum(input_tokens), 0), coalesce(sum(cache_write_tokens), 0),
                coalesce(sum(cache_read_tokens), 0), coalesce(sum(output_tokens), 0),
                coalesce(sum(request_count), 0) FROM inference_calls {where}""",  # noqa: S608 # nosec B608
                tuple(params),
            ).fetchone()
            breakdown_rows = connection.execute(
                f"""SELECT run_id, stage, purpose, model, count(*), sum(status = 'failed'),
                sum(usage_available = 0), coalesce(sum(input_tokens), 0),
                coalesce(sum(cache_write_tokens), 0), coalesce(sum(cache_read_tokens), 0),
                coalesce(sum(output_tokens), 0), coalesce(sum(request_count), 0)
                FROM inference_calls {where}
                GROUP BY run_id, stage, purpose, model ORDER BY run_id, stage, purpose, model""",  # noqa: S608 # nosec B608
                tuple(params),
            ).fetchall()
            selected_run_ids = tuple(
                str(item[0])
                for item in connection.execute(
                    f"SELECT DISTINCT run_id FROM inference_calls {where} ORDER BY run_id",  # noqa: S608 # nosec B608
                    tuple(params),
                ).fetchall()
            )
            completed_units = _completed_units_for_usage(
                connection,
                selected_run_ids=selected_run_ids,
                since=since,
                stage=stage,
            )
        input_tokens = int(row[3])
        cache_read_tokens = int(row[5])
        cache_denominator = input_tokens
        return FilteredInferenceUsage(
            run_id=run_id,
            since=since,
            stage=stage,
            logical_call_count=int(row[0]),
            failed_call_count=int(row[1] or 0),
            unavailable_usage_call_count=int(row[2] or 0),
            completed_unit_count=completed_units,
            input_tokens_per_completed_unit=(None if completed_units == 0 else input_tokens / completed_units),
            cache_read_fraction=(None if cache_denominator == 0 else cache_read_tokens / cache_denominator),
            usage=InferenceUsage(
                input_tokens=input_tokens,
                cache_write_tokens=int(row[4]),
                cache_read_tokens=cache_read_tokens,
                output_tokens=int(row[6]),
                request_count=int(row[7]),
            ),
            breakdown=tuple(
                FilteredInferenceUsageBreakdown(
                    run_id=str(item[0]),
                    stage=str(item[1]),
                    purpose=str(item[2]),
                    model=str(item[3]),
                    logical_call_count=int(item[4]),
                    failed_call_count=int(item[5] or 0),
                    unavailable_usage_call_count=int(item[6] or 0),
                    usage=InferenceUsage(
                        input_tokens=int(item[7]),
                        cache_write_tokens=int(item[8]),
                        cache_read_tokens=int(item[9]),
                        output_tokens=int(item[10]),
                        request_count=int(item[11]),
                    ),
                )
                for item in breakdown_rows
            ),
        )

    def status(
        self,
        source_id: str | None = None,
        *,
        as_of: datetime | None = None,
    ) -> IntelligenceWorkStatus:
        """Return provider-free pending and active work counts."""
        boundary = as_of or datetime.now(tz=timezone.utc)
        with self._database.read_only_transaction() as connection:
            bundles = _count_interpretation_bundles(connection, source_id)
            interpretation = _count_interpretation(connection, source_id)
            discovery = _count_discovery(connection, source_id)
            research = _count_research(connection, source_id, as_of=boundary)
            synthesis = _count_synthesis(connection, source_id)
        return IntelligenceWorkStatus(
            source_id=source_id,
            pending_interpretation_bundles=bundles[0],
            active_interpretation_bundles=bundles[1],
            pending_interpretation_chunks=interpretation[0],
            active_interpretation_chunks=interpretation[1],
            pending_discovery_units=discovery[0],
            active_discovery_units=discovery[1],
            pending_research_jobs=research[0],
            active_research_jobs=research[1],
            due_research_reviews=research[2],
            pending_synthesis_units=synthesis[0],
            active_synthesis_units=synthesis[1],
        )

    def completion_counts_for_run(self, run_id: str) -> IntelligenceWorkCompletionCounts:
        """Count exact work records whose durable completion belongs to one run."""
        with self._database.transaction() as connection:
            interpretation_chunks = int(
                connection.execute(
                    """SELECT count(*) FROM interpretation_bundle_chunks
                    WHERE status = 'completed' AND claimed_run_id = ?""",
                    (run_id,),
                ).fetchone()[0]
            )
            interpretation_bundles = int(
                connection.execute(
                    """SELECT count(*) FROM interpretation_bundles AS bundle
                    WHERE bundle.status = 'completed' AND EXISTS (
                        SELECT 1 FROM interpretation_bundle_chunks AS chunk
                        WHERE chunk.bundle_id = bundle.bundle_id
                          AND chunk.claimed_run_id = ?
                          AND chunk.completed_at = bundle.completed_at
                    )""",
                    (run_id,),
                ).fetchone()[0]
            )
            discovery_units = int(
                connection.execute(
                    """SELECT count(*) FROM discovery_batch_units AS binding
                    JOIN discovery_batches AS batch USING (batch_id)
                    WHERE batch.status = 'completed' AND batch.run_id = ?""",
                    (run_id,),
                ).fetchone()[0]
            )
            research_jobs = int(
                connection.execute(
                    """SELECT count(*) FROM research_jobs
                    WHERE status = 'terminal' AND claimed_run_id = ?""",
                    (run_id,),
                ).fetchone()[0]
            )
            synthesis_units = int(
                connection.execute(
                    """SELECT count(*) FROM synthesis_units
                    WHERE status = 'completed' AND claimed_run_id = ?""",
                    (run_id,),
                ).fetchone()[0]
            )
        return IntelligenceWorkCompletionCounts(
            interpretation_bundles=interpretation_bundles,
            interpretation_chunks=interpretation_chunks,
            discovery_units=discovery_units,
            research_jobs=research_jobs,
            synthesis_units=synthesis_units,
        )

    def durable_advance_for_run(self, run_id: str) -> DurableAdvanceMetric:
        """Count committed transitions, including nonterminal A3 progress."""
        with self._database.read_only_transaction() as connection:
            interpretation = _count_scalar(connection, "interpretation_bundle_chunks", "claimed_run_id", run_id)
            discovery = _count_scalar(connection, "discovery_batches", "run_id", run_id)
            jobs = _count_scalar(connection, "research_jobs", "claimed_run_id", run_id)
            waves = int(
                connection.execute(
                    """SELECT count(*) FROM research_wave_results
                    WHERE run_id = ? OR execution_completed_run_id = ?""",
                    (run_id, run_id),
                ).fetchone()[0]
            )
            checkpoints = _count_scalar(connection, "research_job_checkpoints", "run_id", run_id)
            tasks = int(
                connection.execute(
                    """SELECT count(*) FROM planned_research_tasks AS task
                JOIN research_sessions AS session ON session.session_id = task.materialized_session_id
                WHERE session.run_id = ? AND task.status IN ('materialized', 'completed')""",
                    (run_id,),
                ).fetchone()[0]
            )
            synthesis = _count_scalar(connection, "synthesis_units", "claimed_run_id", run_id)
        values = (interpretation, discovery, jobs, waves, checkpoints, tasks, synthesis)
        return DurableAdvanceMetric(
            run_id=run_id,
            interpretation_chunks=interpretation,
            discovery_batches=discovery,
            research_jobs_created=jobs,
            research_wave_results=waves,
            research_checkpoints=checkpoints,
            research_tasks_materialized=tasks,
            synthesis_units=synthesis,
            total=sum(values),
        )

    def has_interpretation_bundle(
        self,
        *,
        source_item_id: str,
        content_version: str,
        interpreter_version: str,
    ) -> bool:
        """Return whether one evidence bundle is already materialized."""
        with self._database.read_only_transaction() as connection:
            row = connection.execute(
                """SELECT 1 FROM interpretation_bundles
                WHERE source_item_id = ? AND content_version = ? AND interpreter_version = ?
                LIMIT 1""",
                (source_item_id, content_version, interpreter_version),
            ).fetchone()
        return row is not None

    def missing_discovery_unit_count(self, unit_ids: tuple[str, ...]) -> int:
        """Count projected discovery identities absent from the durable ledger."""
        if not unit_ids:
            return 0
        with self._database.read_only_transaction() as connection:
            present = sum(
                int(
                    connection.execute(
                        "SELECT EXISTS(SELECT 1 FROM discovery_units WHERE unit_id = ?)",
                        (unit_id,),
                    ).fetchone()[0]
                )
                for unit_id in unit_ids
            )
        return len(unit_ids) - present


def _insert_or_require(
    connection: sqlite3.Connection, table: str, id_column: str, identifier: str, values: dict[str, object]
) -> None:
    columns = tuple(values)
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    _ = connection.execute(
        f"INSERT OR IGNORE INTO {table} ({column_sql}) VALUES ({placeholders})",  # noqa: S608  # nosec B608
        tuple(values[column] for column in columns),
    )
    row = connection.execute(
        f"SELECT {column_sql} FROM {table} WHERE {id_column} = ?",  # noqa: S608  # nosec B608
        (identifier,),
    ).fetchone()
    if row is None or tuple(row) != tuple(values[column] for column in columns):
        raise ImmutableWorkCollisionError(f"{table} identity is already bound to different content.")


def _insert_initial_lifecycle_record(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    identifier: str,
    initial_values: dict[str, object],
    *,
    immutable_columns: tuple[str, ...],
) -> None:
    """Create lifecycle state once and compare only its semantic immutable fields."""
    columns = tuple(initial_values)
    placeholders = ", ".join("?" for _ in columns)
    _ = connection.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608  # nosec B608
        tuple(initial_values[column] for column in columns),
    )
    row = connection.execute(
        f"SELECT {', '.join(immutable_columns)} FROM {table} WHERE {id_column} = ?",  # noqa: S608  # nosec B608
        (identifier,),
    ).fetchone()
    expected = tuple(initial_values[column] for column in immutable_columns)
    if row is None or tuple(row) != expected:
        raise ImmutableWorkCollisionError(f"{table} semantic identity is already bound to different content.")


def _count_scalar(connection: sqlite3.Connection, table: str, run_column: str, run_id: str) -> int:
    allowed = {
        ("interpretation_bundle_chunks", "claimed_run_id"),
        ("discovery_batches", "run_id"),
        ("research_jobs", "claimed_run_id"),
        ("research_wave_results", "run_id"),
        ("research_job_checkpoints", "run_id"),
        ("synthesis_units", "claimed_run_id"),
    }
    if (table, run_column) not in allowed:
        raise ValueError("unsupported durable advance relation")
    row = connection.execute(
        f"SELECT count(*) FROM {table} WHERE {run_column} = ?",  # noqa: S608 # nosec B608
        (run_id,),
    ).fetchone()
    return int(row[0])


def _completed_units_for_usage(
    connection: sqlite3.Connection,
    *,
    selected_run_ids: tuple[str, ...],
    since: datetime | None,
    stage: str | None,
) -> int:
    if not selected_run_ids:
        return 0
    stage_queries = {
        "A1": ("interpretation_bundle_chunks", "claimed_run_id", "completed_at"),
        "A2": ("discovery_batches", "run_id", "completed_at"),
        "A3": ("research_job_checkpoints", "run_id", "recorded_at"),
        "A4": ("synthesis_units", "claimed_run_id", "completed_at"),
    }
    selected = stage_queries.items() if stage is None else ((stage, stage_queries[stage]),)
    total = 0
    for _, (table, run_column, time_column) in selected:
        clauses = [
            f"{time_column} IS NOT NULL",
            f"{run_column} IN (SELECT value FROM json_each(?))",  # noqa: S608 # nosec B608
        ]
        params: list[object] = [_json(selected_run_ids)]
        if since is not None:
            clauses.append(f"{time_column} >= ?")
            params.append(_time(since))
        row = connection.execute(
            f"SELECT count(*) FROM {table} WHERE {' AND '.join(clauses)}",  # noqa: S608 # nosec B608
            tuple(params),
        ).fetchone()
        total += int(row[0])
    return total


def _require_exact_ids(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    identifiers: tuple[str, ...],
) -> None:
    if not identifiers:
        return
    rows = connection.execute(
        f"SELECT {id_column} FROM {table} WHERE {id_column} IN (SELECT value FROM json_each(?))",  # noqa: S608  # nosec B608
        (_json(identifiers),),
    ).fetchall()
    if tuple(sorted(str(row[0]) for row in rows)) != tuple(sorted(identifiers)):
        raise WorkTransitionError(f"Incremental research admission references unknown {table} identities.")


def _interpretation_chunk(connection: sqlite3.Connection, chunk_id: str) -> InterpretationChunkRecord:
    row = connection.execute("SELECT * FROM interpretation_bundle_chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
    try:
        return InterpretationChunkRecord.model_validate(
            {
                "chunk_id": row["chunk_id"],
                "bundle_id": row["bundle_id"],
                "chunk_number": row["chunk_number"],
                "input_fingerprint": row["input_fingerprint"],
                "status": row["status"],
                "claimed_run_id": row["claimed_run_id"],
                "claimed_at": row["claimed_at"],
                "completed_at": row["completed_at"],
                "output_attempt_ids": json.loads(str(row["output_attempt_ids_json"])),
                "output": json.loads(str(row["output_json"])),
                "payload": json.loads(str(row["chunk_json"])),
            }
        )
    except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MalformedWorkRecordError("Stored interpretation chunk is malformed.") from error


def _active_discovery_batch(
    connection: sqlite3.Connection,
    run_id: str,
    reclaim_before: datetime,
    source_id: str | None,
) -> DiscoveryBatchRecord | None:
    params: tuple[object, ...] = (
        (run_id, _time(reclaim_before)) if source_id is None else (run_id, _time(reclaim_before), source_id)
    )
    source_clause = (
        ""
        if source_id is None
        else """AND EXISTS (
        SELECT 1 FROM discovery_batch_units AS binding JOIN discovery_units AS unit USING (unit_id)
        WHERE binding.batch_id = batch.batch_id AND unit.source_id = ?)"""
    )
    row = connection.execute(
        f"""SELECT batch_id FROM discovery_batches AS batch
        WHERE status IN ('active', 'checkpointed')
          AND (batch.run_id = ? OR batch.created_at <= ?
            OR EXISTS (SELECT 1 FROM run_terminal_events AS terminal
                       WHERE terminal.run_id = batch.run_id)) {source_clause}
        ORDER BY created_at, batch_id LIMIT 1""",  # noqa: S608  # nosec B608 -- fixed internal clause.
        params,
    ).fetchone()
    return None if row is None else _discovery_batch(connection, str(row[0]))


def _discovery_batch(connection: sqlite3.Connection, batch_id: str) -> DiscoveryBatchRecord:
    row = connection.execute("SELECT * FROM discovery_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    unit_rows = connection.execute(
        "SELECT unit_id FROM discovery_batch_units WHERE batch_id = ? ORDER BY unit_id", (batch_id,)
    ).fetchall()
    return DiscoveryBatchRecord.model_validate(
        {
            "batch_id": row["batch_id"],
            "run_id": row["run_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "unit_ids": tuple(str(value[0]) for value in unit_rows),
            "result_fingerprint": row["result_fingerprint"],
            "output_candidate_ids": json.loads(str(row["output_candidate_ids_json"])),
            "validated_output": json.loads(str(row["validated_output_json"])),
        }
    )


def _review_cycle_job_id(parent_job_id: str, review_trigger_at: str) -> str:
    return hashlib.sha256("\x1f".join(("research-review-cycle", parent_job_id, review_trigger_at)).encode()).hexdigest()


def _create_review_cycle(
    connection: sqlite3.Connection,
    *,
    parent_job_id: str,
    successor_job_id: str,
    review_trigger_at: str,
    run_id: str,
    claimed_at: datetime,
) -> None:
    """Create a fresh-budget successor while retaining the terminal parent."""
    parent = connection.execute(
        """SELECT candidate_thesis_id, premise_fingerprint, cycle_number,
                  source_discovery_unit_id, job_json
           FROM research_jobs WHERE job_id = ? AND status = 'terminal'
             AND next_review_at = ?""",
        (parent_job_id, review_trigger_at),
    ).fetchone()
    if parent is None:
        raise WorkTransitionError("Due research parent changed before successor creation.")
    created_at = _time(claimed_at)
    _ = connection.execute(
        """INSERT INTO research_jobs (
            job_id, parent_job_id, candidate_thesis_id, premise_fingerprint,
            cycle_number, review_trigger_at, source_discovery_unit_id, status,
            created_at, claimed_run_id, claimed_at, completed_at, stop_reason,
            wave_count, search_count, accepted_fetch_count, next_review_at, job_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, NULL, 0, 0, 0, NULL, ?)""",
        (
            successor_job_id,
            parent_job_id,
            str(parent[0]),
            str(parent[1]),
            int(parent[2]) + 1,
            review_trigger_at,
            parent[3],
            created_at,
            run_id,
            created_at,
            str(parent[4]),
        ),
    )
    _ = connection.execute(
        """INSERT INTO research_job_discovery_origins (job_id, unit_id)
        SELECT ?, unit_id FROM research_job_discovery_origins WHERE job_id = ?""",
        (successor_job_id, parent_job_id),
    )
    cursor = connection.execute(
        """UPDATE research_jobs SET next_review_at = NULL
        WHERE job_id = ? AND status = 'terminal' AND next_review_at = ?""",
        (parent_job_id, review_trigger_at),
    )
    if cursor.rowcount != 1:
        raise WorkTransitionError("Due research parent changed during successor creation.")


def _research_job(connection: sqlite3.Connection, job_id: str) -> ClaimedResearchJobRecord:
    row = connection.execute("SELECT * FROM research_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return ClaimedResearchJobRecord.model_validate(
        {
            "job_id": row["job_id"],
            "parent_job_id": row["parent_job_id"],
            "candidate_thesis_id": row["candidate_thesis_id"],
            "premise_fingerprint": row["premise_fingerprint"],
            "cycle_number": row["cycle_number"],
            "review_trigger_at": row["review_trigger_at"],
            "source_discovery_unit_id": row["source_discovery_unit_id"],
            "created_at": row["created_at"],
            "payload": json.loads(str(row["job_json"])),
            "status": row["status"],
            "claimed_run_id": row["claimed_run_id"],
            "claimed_at": row["claimed_at"],
            "completed_at": row["completed_at"],
            "stop_reason": row["stop_reason"],
            "wave_count": row["wave_count"],
            "search_count": row["search_count"],
            "accepted_fetch_count": row["accepted_fetch_count"],
            "next_review_at": row["next_review_at"],
        }
    )


def _synthesis_unit(connection: sqlite3.Connection, unit_id: str) -> ClaimedSynthesisUnitRecord:
    row = connection.execute("SELECT * FROM synthesis_units WHERE unit_id = ?", (unit_id,)).fetchone()
    return ClaimedSynthesisUnitRecord.model_validate(
        {
            "unit_id": row["unit_id"],
            "research_job_id": row["research_job_id"],
            "input_fingerprint": row["input_fingerprint"],
            "created_at": row["created_at"],
            "payload": json.loads(str(row["unit_json"])),
            "status": row["status"],
            "claimed_run_id": row["claimed_run_id"],
            "claimed_at": row["claimed_at"],
            "completed_at": row["completed_at"],
            "checkpoint_fingerprint": row["checkpoint_fingerprint"],
            "validated_output": json.loads(str(row["validated_output_json"])),
        }
    )


def _count_interpretation(connection: sqlite3.Connection, source_id: str | None) -> tuple[int, int]:
    clause = "" if source_id is None else "AND item.source_id = ?"
    params = () if source_id is None else (source_id,)
    rows = connection.execute(
        f"""SELECT chunk.status, count(*) FROM interpretation_bundle_chunks AS chunk
        JOIN interpretation_bundles AS bundle USING (bundle_id)
        JOIN source_items AS item ON item.source_item_id = bundle.source_item_id
          AND item.content_version = bundle.content_version
        WHERE chunk.status != 'completed' {clause} GROUP BY chunk.status""",  # noqa: S608  # nosec B608 -- fixed internal clause.
        params,
    ).fetchall()
    return _status_counts(rows)


def _count_interpretation_bundles(connection: sqlite3.Connection, source_id: str | None) -> tuple[int, int]:
    clause = "" if source_id is None else "AND item.source_id = ?"
    params = () if source_id is None else (source_id,)
    rows = connection.execute(
        f"""SELECT bundle.status, count(*) FROM interpretation_bundles AS bundle
        JOIN source_items AS item ON item.source_item_id = bundle.source_item_id
          AND item.content_version = bundle.content_version
        WHERE bundle.status != 'completed' {clause} GROUP BY bundle.status""",  # noqa: S608  # nosec B608 -- fixed internal clause.
        params,
    ).fetchall()
    return _status_counts(rows)


def _count_discovery(connection: sqlite3.Connection, source_id: str | None) -> tuple[int, int]:
    clause = "" if source_id is None else "AND source_id = ?"
    params = () if source_id is None else (source_id,)
    rows = connection.execute(
        f"SELECT status, count(*) FROM discovery_units WHERE status != 'completed' {clause} GROUP BY status",  # noqa: S608  # nosec B608 -- clause is selected from fixed internal SQL.
        params,
    ).fetchall()
    return _status_counts(rows)


def _count_research(
    connection: sqlite3.Connection,
    source_id: str | None,
    *,
    as_of: datetime,
) -> tuple[int, int, int]:
    clause = (
        ""
        if source_id is None
        else """AND EXISTS (
        SELECT 1 FROM research_job_discovery_origins AS origin
        JOIN discovery_units AS unit USING (unit_id)
        WHERE origin.job_id = job.job_id AND unit.source_id = ?)"""
    )
    params = () if source_id is None else (source_id,)
    rows = connection.execute(
        f"SELECT status, count(*) FROM research_jobs AS job WHERE status != 'terminal' {clause} GROUP BY status",  # noqa: S608  # nosec B608 -- clause is selected from fixed internal SQL.
        params,
    ).fetchall()
    pending, active = _status_counts(rows)
    due_row = connection.execute(
        f"""SELECT count(*) FROM research_jobs AS job
        WHERE status = 'terminal' AND next_review_at IS NOT NULL AND next_review_at <= ? {clause}""",  # noqa: S608  # nosec B608 -- clause is selected from fixed internal SQL.
        (_time(as_of), *params),
    ).fetchone()
    return pending, active, int(due_row[0])


def _count_synthesis(connection: sqlite3.Connection, source_id: str | None) -> tuple[int, int]:
    clause = (
        ""
        if source_id is None
        else """AND EXISTS (
        SELECT 1 FROM research_job_discovery_origins AS origin
        JOIN discovery_units AS discovery USING (unit_id)
        WHERE origin.job_id = job.job_id AND discovery.source_id = ?)"""
    )
    params = () if source_id is None else (source_id,)
    rows = connection.execute(
        f"""SELECT unit.status, count(*) FROM synthesis_units AS unit
        JOIN research_jobs AS job ON job.job_id = unit.research_job_id
        WHERE unit.status != 'completed' {clause} GROUP BY unit.status""",  # noqa: S608  # nosec B608 -- fixed internal clause.
        params,
    ).fetchall()
    return _status_counts(rows)


def _status_counts(rows: list[sqlite3.Row]) -> tuple[int, int]:
    counts = {str(row[0]): int(row[1]) for row in rows}
    return counts.get("pending", 0), counts.get("active", 0) + counts.get("checkpointed", 0)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Durable work timestamps must be timezone-aware.")
    return value.isoformat()


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else _time(value)


def _require_reclaim_boundary(claimed_at: datetime, reclaim_before: datetime) -> None:
    """Require an aware reclaim boundary that does not follow the claim time."""
    _ = _time(claimed_at)
    _ = _time(reclaim_before)
    if reclaim_before > claimed_at:
        raise ValueError("reclaim_before cannot follow the claim time")
