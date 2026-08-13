"""Module atomically admitting intelligence records with their stage artifact."""

# pyright: reportAny=false

import hashlib
import json
import sqlite3
from datetime import datetime
from datetime import timezone
from typing import ClassVar
from typing import cast

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from money_pit.claims.repository import append_observation_record
from money_pit.claims.repository import append_resolution_record
from money_pit.claims.repository import append_verification_record
from money_pit.portfolio.theses import append_contribution_record
from money_pit.portfolio.theses import append_revision_record
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimResolutionDecision
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.research import ResearchStageAdmission
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import StageArtifactRecord
from money_pit.schemas.runs import bind_artifact_record
from money_pit.schemas.temporal import SignalContribution
from money_pit.schemas.theses import ThesisRevision
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError
from money_pit.storage.intelligence_work import SynthesisOutputRecord
from money_pit.storage.research_semantics import ResearchSemanticPayloadError
from money_pit.storage.research_semantics import canonical_research_context
from money_pit.storage.research_semantics import canonical_research_payload
from money_pit.storage.runs import append_stage_artifact_record


class IntelligenceAdmissionError(StorageError):
    """Raised when a stage batch cannot be admitted as one durable unit."""


class InterpretationAdmission(BaseModel):
    """One pending interpretation attempt and all observations it produced."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    attempt_id: str = Field(min_length=1)
    observations: tuple[ClaimObservation, ...] = ()


class IntelligenceAdmissionRepository:
    """Commit A1 and A4 intelligence batches atomically with authoritative artifacts."""

    def __init__(self, database: Database) -> None:
        """Bind admission to the shared SQLite transaction authority."""
        self._database: Database = database

    def admit_incremental_research_artifact(self, artifact: StageArtifactRecord) -> None:
        """Append an A3 summary without rewriting prior-run descendants."""
        if artifact.stage != "A3" or artifact.output_ids:
            raise IntelligenceAdmissionError(
                "Incremental research artifacts must be A3 summaries without new domain outputs."
            )
        with self._database.transaction(TransactionMode.WRITE) as connection:
            append_stage_artifact_record(connection, artifact)

    def admit_interpretation(
        self,
        admissions: tuple[InterpretationAdmission, ...],
        *,
        completed_at: datetime,
        known_at: datetime,
        artifact: StageArtifactRecord,
        bundle_id: str | None = None,
    ) -> None:
        """Commit pending attempts, observations, and the A1 artifact together."""
        if artifact.stage != "A1":
            raise IntelligenceAdmissionError("Interpretation admission requires an A1 artifact.")
        if len({item.attempt_id for item in admissions}) != len(admissions):
            raise IntelligenceAdmissionError("Interpretation attempt IDs must be unique.")
        observation_ids = tuple(
            observation.observation_id for admission in admissions for observation in admission.observations
        )
        if len(set(observation_ids)) != len(observation_ids):
            raise IntelligenceAdmissionError("Interpretation observation IDs must be unique.")
        expected_outputs = (
            *(bind_artifact_record(ArtifactRecordKind.INTERPRETATION_ATTEMPT, item.attempt_id) for item in admissions),
            *(
                bind_artifact_record(ArtifactRecordKind.OBSERVATION, observation_id)
                for observation_id in observation_ids
            ),
        )
        if artifact.output_ids != expected_outputs:
            raise IntelligenceAdmissionError("A1 artifact outputs must equal admitted attempts and observations.")
        completed_text = _utc_text(completed_at)
        known_text = _utc_text(known_at)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            for admission in admissions:
                _admit_interpretation_attempt(
                    connection,
                    admission,
                    run_id=artifact.run_id,
                    completed_text=completed_text,
                    known_text=known_text,
                )
            append_stage_artifact_record(connection, artifact)
            if bundle_id is not None:
                remaining = connection.execute(
                    "SELECT count(*) FROM interpretation_bundle_chunks WHERE bundle_id = ? AND status != 'completed'",
                    (bundle_id,),
                ).fetchone()
                if remaining is None or int(remaining[0]) != 0:
                    raise IntelligenceAdmissionError("Interpretation bundle has unfinished chunks.")
                cursor = connection.execute(
                    """UPDATE interpretation_bundles SET status = 'completed', completed_at = ?
                    WHERE bundle_id = ? AND status != 'completed'""",
                    (known_text, bundle_id),
                )
                if cursor.rowcount != 1:
                    raise IntelligenceAdmissionError("Interpretation bundle cannot be completed.")

    def admit_research_interpretation(
        self,
        admission: InterpretationAdmission,
        *,
        completed_at: datetime,
        known_at: datetime,
        run_id: str,
    ) -> None:
        """Commit one A3 interpretation attempt without inventing an A1 artifact."""
        completed_text = _utc_text(completed_at)
        known_text = _utc_text(known_at)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _admit_interpretation_attempt(
                connection,
                admission,
                run_id=run_id,
                completed_text=completed_text,
                known_text=known_text,
            )

    def admit_synthesis(
        self,
        *,
        resolutions: tuple[ClaimResolutionDecision, ...],
        verifications: tuple[VerificationResult, ...],
        revisions: tuple[ThesisRevision, ...],
        contributions: tuple[SignalContribution, ...],
        artifact: StageArtifactRecord,
    ) -> None:
        """Commit the complete A4 intelligence batch and artifact together."""
        if artifact.stage != "A4":
            raise IntelligenceAdmissionError("Synthesis admission requires an A4 artifact.")
        output_ids = (
            *(bind_artifact_record(ArtifactRecordKind.CLAIM_RESOLUTION, item.decision_id) for item in resolutions),
            *(bind_artifact_record(ArtifactRecordKind.VERIFICATION, item.verification_id) for item in verifications),
            *(bind_artifact_record(ArtifactRecordKind.THESIS_REVISION, item.revision_id) for item in revisions),
            *(
                bind_artifact_record(ArtifactRecordKind.SIGNAL_CONTRIBUTION, item.contribution_id)
                for item in contributions
            ),
        )
        if len(set(output_ids)) != len(output_ids):
            raise IntelligenceAdmissionError("A4 output record IDs must be unique.")
        if artifact.output_ids != output_ids:
            raise IntelligenceAdmissionError("A4 artifact outputs must equal admitted intelligence records.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            for decision in resolutions:
                append_resolution_record(connection, decision)
            for verification in verifications:
                append_verification_record(connection, verification)
            for revision in revisions:
                append_revision_record(connection, revision)
            for contribution in contributions:
                append_contribution_record(connection, contribution)
            append_stage_artifact_record(connection, artifact)

    def admit_synthesis_unit(
        self,
        *,
        resolutions: tuple[ClaimResolutionDecision, ...],
        verifications: tuple[VerificationResult, ...],
        revisions: tuple[ThesisRevision, ...],
        contributions: tuple[SignalContribution, ...],
        artifact: StageArtifactRecord,
        unit_id: str,
        output: SynthesisOutputRecord,
    ) -> None:
        """Atomically admit A4 domain records, artifact, and completed work unit."""
        _require_synthesis_outputs(
            resolutions=resolutions,
            verifications=verifications,
            revisions=revisions,
            contributions=contributions,
            artifact=artifact,
        )
        with self._database.transaction(TransactionMode.WRITE) as connection:
            unit = connection.execute(
                """SELECT status, claimed_run_id, unit_payload_hash, unit_json
                FROM synthesis_units WHERE unit_id = ?""",
                (unit_id,),
            ).fetchone()
            if unit is None or str(unit[0]) not in {"active", "checkpointed"} or str(unit[1]) != artifact.run_id:
                raise IntelligenceAdmissionError("Synthesis unit is not claimed by the artifact run.")
            if hashlib.sha256(str(unit[3]).encode()).hexdigest() != str(unit[2]):
                raise IntelligenceAdmissionError("Synthesis unit semantic context changed after admission.")
            for decision in resolutions:
                append_resolution_record(connection, decision)
            for verification in verifications:
                append_verification_record(connection, verification)
            for revision in revisions:
                append_revision_record(connection, revision)
            for contribution in contributions:
                append_contribution_record(connection, contribution)
            append_stage_artifact_record(connection, artifact)
            encoded_ids = _json_ids(output.output_record_ids)
            encoded_output = _canonical_json(output.payload)
            _ = connection.execute(
                """INSERT OR IGNORE INTO synthesis_outputs
                (output_id, unit_id, output_fingerprint, created_at,
                 output_record_ids_json, output_json) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    output.output_id,
                    unit_id,
                    output.output_fingerprint,
                    _utc_text(output.created_at),
                    encoded_ids,
                    encoded_output,
                ),
            )
            stored = connection.execute(
                """SELECT unit_id, output_fingerprint, created_at,
                          output_record_ids_json, output_json
                   FROM synthesis_outputs WHERE output_id = ?""",
                (output.output_id,),
            ).fetchone()
            expected = (unit_id, output.output_fingerprint, _utc_text(output.created_at), encoded_ids, encoded_output)
            if stored is None or tuple(stored) != expected:
                raise IntelligenceAdmissionError("Synthesis output identity collides with different content.")
            cursor = connection.execute(
                """UPDATE synthesis_units SET status = 'completed', completed_at = ?
                WHERE unit_id = ? AND claimed_run_id = ? AND status IN ('active', 'checkpointed')""",
                (_utc_text(artifact.known_at), unit_id, artifact.run_id),
            )
            if cursor.rowcount != 1:
                raise IntelligenceAdmissionError("Synthesis unit changed during atomic admission.")

    def admit_research_stage(
        self,
        admission: ResearchStageAdmission,
        *,
        artifact: StageArtifactRecord,
    ) -> None:
        """Atomically expose a complete staged A3 run through its exact artifact."""
        if artifact.stage != "A3":
            raise IntelligenceAdmissionError("Research-stage admission requires an A3 artifact.")
        if admission.run_id != artifact.run_id or admission.known_at != artifact.known_at:
            raise IntelligenceAdmissionError("A3 admission and artifact boundaries do not match.")
        if artifact.input_ids != admission.input_ids():
            raise IntelligenceAdmissionError("A3 artifact inputs must equal admitted candidates.")
        if artifact.output_ids != admission.output_ids():
            raise IntelligenceAdmissionError("A3 artifact outputs must equal all admitted staged records.")
        if admission.payload_hash != artifact.payload_hash():
            raise IntelligenceAdmissionError("A3 admission payload hash does not match its artifact.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _require_research_stage_records(connection, admission)
            _require_research_semantics(connection, admission, artifact)
            append_stage_artifact_record(connection, artifact)
            encoded = admission.model_dump_json()
            _ = connection.execute(
                """
                INSERT INTO research_stage_admissions (
                    admission_id, run_id, artifact_id, known_at, payload_hash,
                    semantic_context_hash, admission_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    artifact.artifact_id,
                    admission.run_id,
                    artifact.artifact_id,
                    _utc_text(admission.known_at),
                    admission.payload_hash,
                    admission.semantic_context_hash,
                    encoded,
                ),
            )
            row = connection.execute(
                """SELECT artifact_id, admission_json FROM research_stage_admissions
                WHERE run_id = ?""",
                (admission.run_id,),
            ).fetchone()
            if row is None or str(row[0]) != artifact.artifact_id or str(row[1]) != encoded:
                raise IntelligenceAdmissionError("A3 run is already bound to another admission.")

    def research_stage_admission(self, run_id: str) -> ResearchStageAdmission | None:
        """Return the authoritative A3 admission marker for recovery and replay."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT admission.artifact_id, admission.known_at,
                          admission.admission_json, artifact.run_id, artifact.stage,
                          admission.payload_hash, admission.semantic_context_hash
                FROM research_stage_admissions AS admission
                JOIN stage_artifacts AS artifact USING (artifact_id)
                WHERE admission.run_id = ?""",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            admission = ResearchStageAdmission.model_validate_json(str(row[2]))
        except ValidationError as error:
            raise IntelligenceAdmissionError("Stored A3 admission is malformed.") from error
        if (
            admission.run_id != run_id
            or _utc_text(admission.known_at) != str(row[1])
            or str(row[3]) != run_id
            or str(row[4]) != "A3"
            or admission.payload_hash != str(row[5])
            or admission.semantic_context_hash != str(row[6])
        ):
            raise IntelligenceAdmissionError("Stored A3 admission bindings are inconsistent.")
        return admission


def _admit_interpretation_attempt(
    connection: sqlite3.Connection,
    admission: InterpretationAdmission,
    *,
    run_id: str,
    completed_text: str,
    known_text: str,
) -> None:
    row = connection.execute(
        """
        SELECT source_item_id, run_id, outcome, completed_at, known_at,
               observation_ids_json
        FROM claim_interpretation_attempts WHERE attempt_id = ?
        """,
        (admission.attempt_id,),
    ).fetchone()
    if row is None or str(row[1]) != run_id:
        raise IntelligenceAdmissionError(
            f"Interpretation attempt is unknown or belongs to another run: {admission.attempt_id}"
        )
    admission_ids = tuple(item.observation_id for item in admission.observations)
    for observation in admission.observations:
        if observation.source_item_id != str(row[0]):
            raise IntelligenceAdmissionError(
                f"Observation belongs to another source item: {observation.observation_id}"
            )
        append_observation_record(connection, observation)
    if str(row[2]) == "succeeded":
        if (
            str(row[3]) != completed_text
            or str(row[4]) != known_text
            or tuple(json.loads(str(row[5]))) != admission_ids
        ):
            raise IntelligenceAdmissionError(
                f"Interpretation attempt already has different completion: {admission.attempt_id}"
            )
        return
    if str(row[2]) != "pending":
        raise IntelligenceAdmissionError(f"Interpretation attempt is not pending: {admission.attempt_id}")
    cursor = connection.execute(
        """
        UPDATE claim_interpretation_attempts
        SET completed_at = ?, known_at = ?, outcome = 'succeeded',
            observation_ids_json = ?
        WHERE attempt_id = ? AND outcome = 'pending'
        """,
        (completed_text, known_text, _json_ids(admission_ids), admission.attempt_id),
    )
    if cursor.rowcount != 1:
        raise IntelligenceAdmissionError(f"Interpretation attempt changed during admission: {admission.attempt_id}")


def _require_research_stage_records(
    connection: sqlite3.Connection,
    admission: ResearchStageAdmission,
) -> None:
    session_rows = connection.execute(
        """SELECT session_id, scope_kind, scope_subject_id, status
        FROM research_sessions WHERE run_id = ? ORDER BY session_id""",
        (admission.run_id,),
    ).fetchall()
    session_ids = tuple(str(row[0]) for row in session_rows)
    if session_ids != tuple(sorted(admission.session_ids)):
        raise IntelligenceAdmissionError("A3 admission must bind every run-owned research session.")
    if any(str(row[3]) == "active" for row in session_rows):
        raise IntelligenceAdmissionError("A3 admission requires terminal research sessions.")
    candidate_session_ids = {str(row[2]) for row in session_rows if str(row[1]) == "candidate_thesis"}
    if not candidate_session_ids.issubset(set(admission.candidate_thesis_ids)):
        raise IntelligenceAdmissionError("A3 candidate sessions are not bound as artifact inputs.")

    planned_rows = connection.execute(
        """SELECT task_id, candidate_thesis_id, materialized_session_id,
                  materialized_task_id, status
        FROM planned_research_tasks
        WHERE run_id = ? AND status = 'completed' ORDER BY task_id""",
        (admission.run_id,),
    ).fetchall()
    if tuple(str(row[0]) for row in planned_rows) != tuple(sorted(admission.planned_task_ids)):
        raise IntelligenceAdmissionError("A3 admission must bind every completed planned task.")
    if any(
        str(row[2]) not in admission.session_ids
        or str(row[3]) not in admission.task_ids
        or str(row[1]) not in admission.candidate_thesis_ids
        or str(row[4]) != "completed"
        for row in planned_rows
    ):
        raise IntelligenceAdmissionError("A3 planned-task materialization is inconsistent.")

    _require_exact_descendants(
        connection,
        table="research_tasks",
        id_column="task_id",
        parent_column="session_id",
        parent_ids=admission.session_ids,
        expected_ids=admission.task_ids,
        terminal_statuses=("completed", "failed"),
    )
    _require_exact_descendants(
        connection,
        table="research_results",
        id_column="result_id",
        parent_column="task_id",
        parent_ids=admission.task_ids,
        expected_ids=admission.result_ids,
    )
    _require_exact_descendants(
        connection,
        table="research_fetches",
        id_column="fetch_id",
        parent_column="task_id",
        parent_ids=admission.task_ids,
        expected_ids=admission.fetch_ids,
    )
    _require_exact_descendants(
        connection,
        table="research_failures",
        id_column="failure_id",
        parent_column="session_id",
        parent_ids=admission.session_ids,
        expected_ids=admission.failure_ids,
    )
    _require_exact_descendants(
        connection,
        table="research_stop_events",
        id_column="stop_event_id",
        parent_column="session_id",
        parent_ids=admission.session_ids,
        expected_ids=admission.stop_event_ids,
    )
    _require_exact_fetch_provenance(connection, admission)
    _require_exact_interpretations(connection, admission)


def _require_exact_descendants(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    parent_column: str,
    parent_ids: tuple[str, ...],
    expected_ids: tuple[str, ...],
    terminal_statuses: tuple[str, ...] = (),
) -> None:
    selected_columns = f"{id_column}{', status' if terminal_statuses else ''}"
    # Table and column identifiers are selected by internal admission code.
    query = f"SELECT {selected_columns} FROM {table} WHERE {parent_column} IN (SELECT value FROM json_each(?)) ORDER BY {id_column}"  # noqa: S608  # nosec B608
    rows = connection.execute(
        query,
        (_json_ids(parent_ids),),
    ).fetchall()
    if tuple(str(row[0]) for row in rows) != tuple(sorted(expected_ids)):
        raise IntelligenceAdmissionError(f"A3 admission does not exactly bind {table}.")
    if terminal_statuses and any(str(row[1]) not in terminal_statuses for row in rows):
        raise IntelligenceAdmissionError(f"A3 admission contains nonterminal {table}.")


def _require_exact_fetch_provenance(
    connection: sqlite3.Connection,
    admission: ResearchStageAdmission,
) -> None:
    fetch_rows = connection.execute(
        """SELECT source_item_id, asset_id FROM research_fetches
        WHERE fetch_id IN (SELECT value FROM json_each(?)) AND status = 'succeeded'""",
        (_json_ids(admission.fetch_ids),),
    ).fetchall()
    source_ids = tuple(sorted({str(row[0]) for row in fetch_rows if row[0] is not None}))
    asset_ids = tuple(sorted({str(row[1]) for row in fetch_rows if row[1] is not None}))
    if source_ids != tuple(sorted(admission.source_item_ids)):
        raise IntelligenceAdmissionError("A3 admission source items do not match successful fetches.")
    if asset_ids != tuple(sorted(admission.asset_ids)):
        raise IntelligenceAdmissionError("A3 admission assets do not match successful fetches.")
    document_rows = connection.execute(
        """SELECT DISTINCT document_id FROM evidence_processing_attempts
        WHERE asset_id IN (SELECT value FROM json_each(?)) AND status = 'succeeded'
          AND document_id IS NOT NULL ORDER BY document_id""",
        (_json_ids(admission.asset_ids),),
    ).fetchall()
    if tuple(str(row[0]) for row in document_rows) != tuple(sorted(admission.document_ids)):
        raise IntelligenceAdmissionError("A3 admission documents do not match processed assets.")
    fragment_rows = connection.execute(
        """SELECT fragment_id FROM evidence_fragments
        WHERE asset_id IN (SELECT value FROM json_each(?)) ORDER BY fragment_id""",
        (_json_ids(admission.asset_ids),),
    ).fetchall()
    if tuple(str(row[0]) for row in fragment_rows) != tuple(sorted(admission.fragment_ids)):
        raise IntelligenceAdmissionError("A3 admission fragments do not match fetched assets.")


def _require_exact_interpretations(
    connection: sqlite3.Connection,
    admission: ResearchStageAdmission,
) -> None:
    rows = connection.execute(
        """SELECT attempt_id, outcome, observation_ids_json
        FROM claim_interpretation_attempts
        WHERE attempt_id IN (SELECT value FROM json_each(?)) AND run_id = ?
        ORDER BY attempt_id""",
        (_json_ids(admission.interpretation_attempt_ids), admission.run_id),
    ).fetchall()
    if tuple(str(row[0]) for row in rows) != tuple(sorted(admission.interpretation_attempt_ids)):
        raise IntelligenceAdmissionError("A3 admission contains unknown interpretation attempts.")
    if any(str(row[1]) == "pending" for row in rows):
        raise IntelligenceAdmissionError("A3 admission contains pending interpretation attempts.")
    observation_ids = tuple(
        sorted(identifier for row in rows if str(row[1]) == "succeeded" for identifier in json.loads(str(row[2])))
    )
    if observation_ids != tuple(sorted(admission.observation_ids)):
        raise IntelligenceAdmissionError("A3 observations do not match successful attempts.")


def _require_research_semantics(
    connection: sqlite3.Connection,
    admission: ResearchStageAdmission,
    artifact: StageArtifactRecord,
) -> None:
    rows = connection.execute(
        """SELECT summary.summary_id, summary.summary_hash, summary.summary_json
        FROM research_candidate_summaries AS summary
        JOIN research_sessions AS session USING (session_id)
        WHERE summary.run_id = ?
        ORDER BY session.session_id, summary.summary_id""",
        (admission.run_id,),
    ).fetchall()
    if tuple(str(row[0]) for row in rows) != admission.summary_ids:
        raise IntelligenceAdmissionError("A3 admission does not bind every session summary.")
    summary_json_values: list[str] = []
    for row in rows:
        encoded = str(row[2])
        if hashlib.sha256(encoded.encode()).hexdigest() != str(row[1]):
            raise IntelligenceAdmissionError("A3 candidate summary hash is invalid.")
        try:
            parsed = cast("object", json.loads(encoded))
        except json.JSONDecodeError as error:
            raise IntelligenceAdmissionError("A3 candidate summary JSON is invalid.") from error
        if _canonical_json(parsed) != encoded:
            raise IntelligenceAdmissionError("A3 candidate summary JSON is not canonical.")
        summary_json_values.append(encoded)
    try:
        reconstructed = canonical_research_payload(tuple(summary_json_values))
    except ResearchSemanticPayloadError as error:
        raise IntelligenceAdmissionError("A3 candidate summary is invalid.") from error
    if _canonical_json(reconstructed) != _canonical_json(artifact.payload):
        raise IntelligenceAdmissionError("A3 artifact payload does not match durable summaries.")
    context = canonical_research_context(connection, admission)
    if hashlib.sha256(context.encode()).hexdigest() != admission.semantic_context_hash:
        raise IntelligenceAdmissionError("A3 semantic context hash does not match durable rows.")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )


def _require_synthesis_outputs(
    *,
    resolutions: tuple[ClaimResolutionDecision, ...],
    verifications: tuple[VerificationResult, ...],
    revisions: tuple[ThesisRevision, ...],
    contributions: tuple[SignalContribution, ...],
    artifact: StageArtifactRecord,
) -> None:
    """Require exact A4 artifact bindings before a transaction begins."""
    if artifact.stage != "A4":
        raise IntelligenceAdmissionError("Synthesis admission requires an A4 artifact.")
    output_ids = (
        *(bind_artifact_record(ArtifactRecordKind.CLAIM_RESOLUTION, item.decision_id) for item in resolutions),
        *(bind_artifact_record(ArtifactRecordKind.VERIFICATION, item.verification_id) for item in verifications),
        *(bind_artifact_record(ArtifactRecordKind.THESIS_REVISION, item.revision_id) for item in revisions),
        *(bind_artifact_record(ArtifactRecordKind.SIGNAL_CONTRIBUTION, item.contribution_id) for item in contributions),
    )
    if len(set(output_ids)) != len(output_ids):
        raise IntelligenceAdmissionError("A4 output record IDs must be unique.")
    if artifact.output_ids != output_ids:
        raise IntelligenceAdmissionError("A4 artifact outputs must equal admitted intelligence records.")


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("admission timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _json_ids(values: tuple[str, ...]) -> str:
    return json.dumps(values, separators=(",", ":"))
