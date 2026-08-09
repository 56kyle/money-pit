"""Module adapting planned research work to bounded execution sessions."""

from __future__ import annotations

import hashlib
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING
from typing import cast

from money_pit.contracts import ResearchRoundExecution
from money_pit.contracts import ResearchTaskDraft
from money_pit.evidence.aliases import project_evidence
from money_pit.schemas.research import CandidateThesisResearchScope
from money_pit.schemas.research import EvidenceAliasBinding
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import RecoveredResearchStage
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.research import ResearchEvidenceRecord
from money_pit.schemas.research import ResearchQuery
from money_pit.schemas.research import ResearchScope
from money_pit.schemas.research import ResearchSession
from money_pit.schemas.research import ResearchStageAdmission
from money_pit.schemas.research import ResearchStopReason
from money_pit.schemas.research import ResearchTask
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


if TYPE_CHECKING:
    import sqlite3

    from pydantic import JsonValue

    from money_pit.pipeline.interpretation import InterpretationOutcome
    from money_pit.pipeline.interpretation import InterpretationService
    from money_pit.research.assessment import ClaimVerificationMaterialAssessor
    from money_pit.research.repository import ResearchRepository
    from money_pit.research.service import ResearchRoundResult
    from money_pit.research.service import ResearchService
    from money_pit.schemas.theses import CandidateThesis


class PlannedResearchTaskStore:
    """Persist A2 research plans separately from live A3 sessions."""

    def __init__(self, database: Database) -> None:
        """Bind planned work to an initialized database."""
        self._database: Database = database

    def append_task(
        self,
        task: ResearchTaskDraft,
        *,
        run_id: str,
        known_at: datetime,
    ) -> str:
        """Append one immutable run-scoped planned task idempotently."""
        candidate_id: str | None = task.candidate_thesis_id
        if candidate_id is None:
            raise ValueError("Planned research task must identify a candidate")
        task_json: str = task.model_dump_json()
        task_id: str = _planned_task_id(run_id, candidate_id, task_json)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """
                INSERT INTO planned_research_tasks (
                    task_id, candidate_thesis_id, status, created_at,
                    known_at, run_id, task_json
                ) VALUES (?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    task_id,
                    candidate_id,
                    _utc_text(known_at),
                    _utc_text(known_at),
                    run_id,
                    task_json,
                ),
            )
            if cursor.rowcount == 0:
                row: sqlite3.Row | None = cast(
                    "sqlite3.Row | None",
                    connection.execute(
                        "SELECT task_json, run_id FROM planned_research_tasks WHERE task_id = ?",
                        (task_id,),
                    ).fetchone(),
                )
                if row is None or str(_column(row, "task_json")) != task_json or str(_column(row, "run_id")) != run_id:
                    raise ValueError("Planned research task identity collision")
        return task_id

    def pending_for_candidate(
        self,
        candidate_thesis_id: str,
        *,
        run_id: str,
        as_of: datetime,
    ) -> tuple[ResearchTaskDraft, ...]:
        """Return point-in-time pending plans for one candidate and run."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """
                    SELECT task_json FROM planned_research_tasks
                    WHERE candidate_thesis_id = ? AND run_id = ?
                      AND status = 'pending' AND known_at <= ?
                    ORDER BY known_at, created_at, task_id
                    """,
                    (candidate_thesis_id, run_id, _utc_text(as_of)),
                ).fetchall(),
            )
        return tuple(ResearchTaskDraft.model_validate_json(str(_column(row, "task_json"))) for row in rows)

    def materialize(
        self,
        task: ResearchTaskDraft,
        *,
        run_id: str,
        session_id: str,
        round_number: int,
        as_of: datetime,
    ) -> tuple[str, ResearchTask]:
        """Bind one plan to a real session and return its execution contract."""
        candidate_id: str | None = task.candidate_thesis_id
        if candidate_id is None:
            raise ValueError("Research task must identify a candidate")
        task_json: str = task.model_dump_json()
        planned_id: str = _planned_task_id(run_id, candidate_id, task_json)
        execution_task = ResearchTask(
            task_id=f"execution:{planned_id}:{round_number}",
            session_id=session_id,
            round_number=round_number,
            query=ResearchQuery(
                provider=task.provider,
                query_text=task.query,
                candidate_thesis_id=candidate_id,
                purpose=task.purpose,
                material_claim_keys=task.material_claim_keys,
                requested_at=as_of,
                max_results=task.maximum_results,
            ),
            created_at=as_of,
        )
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """
                UPDATE planned_research_tasks
                SET status = 'materialized', materialized_session_id = ?, materialized_task_id = ?
                WHERE task_id = ? AND status = 'pending'
                """,
                (session_id, execution_task.task_id, planned_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(planned_id)
        return planned_id, execution_task

    def complete(self, planned_ids: tuple[str, ...], *, completed_at: datetime) -> None:
        """Mark materialized plans complete after their durable round succeeds."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            for planned_id in planned_ids:
                cursor = connection.execute(
                    """
                    UPDATE planned_research_tasks
                    SET status = 'completed', completed_at = ?
                    WHERE task_id = ? AND status = 'materialized'
                    """,
                    (_utc_text(completed_at), planned_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(planned_id)

    def release(self, planned_ids: tuple[str, ...]) -> None:
        """Return interrupted materialized work to the resumable pending queue."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            for planned_id in planned_ids:
                cursor = connection.execute(
                    """
                    UPDATE planned_research_tasks
                    SET status = 'pending', materialized_session_id = NULL, materialized_task_id = NULL
                    WHERE task_id = ? AND status = 'materialized'
                    """,
                    (planned_id,),
                )
                if cursor.rowcount != 1:
                    raise KeyError(planned_id)


class DurableResearchRoundRunner:
    """Adapt harness research rounds to persistent sessions and providers."""

    def __init__(
        self,
        service: ResearchService,
        repository: ResearchRepository,
        planned_tasks: PlannedResearchTaskStore,
        interpreter: InterpretationService | None = None,
        material_assessor: ClaimVerificationMaterialAssessor | None = None,
    ) -> None:
        """Bind the service, session repository, and A2 plan queue."""
        self._service: ResearchService = service
        self._repository: ResearchRepository = repository
        self._planned_tasks: PlannedResearchTaskStore = planned_tasks
        self._interpreter: InterpretationService | None = interpreter
        self._material_assessor: ClaimVerificationMaterialAssessor | None = material_assessor

    def start_session(
        self,
        *,
        run_id: str,
        candidate: CandidateThesis,
        started_at: datetime,
        deadline: datetime,
        maximum_rounds: int,
        maximum_queries: int,
        maximum_fetches: int,
    ) -> str:
        """Create the real bounded session at the harness start time."""
        return self.start_scoped_session(
            run_id=run_id,
            scope=CandidateThesisResearchScope(
                candidate_thesis_id=candidate.candidate_thesis_id,
            ),
            started_at=started_at,
            deadline=deadline,
            maximum_rounds=maximum_rounds,
            maximum_queries=maximum_queries,
            maximum_fetches=maximum_fetches,
        )

    def start_scoped_session(
        self,
        *,
        run_id: str,
        scope: ResearchScope,
        started_at: datetime,
        deadline: datetime,
        maximum_rounds: int,
        maximum_queries: int,
        maximum_fetches: int,
    ) -> str:
        """Create one bounded session for a candidate, canonical claim, or observation."""
        identity: str = hashlib.sha256(
            f"{run_id}\0{scope.model_dump_json()}\0{started_at.isoformat()}".encode(),
        ).hexdigest()
        session_id: str = f"research:{identity}"
        _ = self._service.start(
            ResearchSession(
                session_id=session_id,
                run_id=run_id,
                scope=scope,
                started_at=started_at,
                deadline_at=deadline,
                maximum_rounds=maximum_rounds,
                maximum_queries=maximum_queries,
                maximum_fetches=maximum_fetches,
            ),
        )
        return session_id

    def run_round(
        self,
        *,
        session_id: str,
        run_id: str,
        candidate: CandidateThesis,
        round_number: int,
        tasks: tuple[ResearchTaskDraft, ...],
        requested_as_of: datetime,
        decision_at: datetime,
        historical_explicit: bool,
        query_budget: int,
        fetch_budget: int,
    ) -> ResearchRoundExecution:
        """Materialize plans, execute providers, and complete durable queue rows."""
        del candidate
        if len(tasks) > query_budget:
            raise ValueError("Research tasks exceed the assigned query budget")
        if sum(task.maximum_results for task in tasks) > fetch_budget:
            raise ValueError("Research tasks exceed the assigned fetch budget")
        for task in tasks:
            _ = self._planned_tasks.append_task(task, run_id=run_id, known_at=decision_at)
        materialized = tuple(
            self._planned_tasks.materialize(
                task,
                run_id=run_id,
                session_id=session_id,
                round_number=round_number,
                as_of=decision_at,
            )
            for task in tasks
        )
        try:
            before = self._repository.budget_state(session_id)
            result = self._service.run_round(
                self._repository.budget_state(session_id).session,
                tuple(item[1] for item in materialized),
                requested_as_of=requested_as_of,
                decision_at=decision_at,
                historical_explicit=historical_explicit,
            )
            after = self._repository.budget_state(session_id)
            query_count: int = after.query_count - before.query_count
            fetch_count: int = after.fetch_count - before.fetch_count
            if query_count > query_budget or fetch_count > fetch_budget:
                raise ValueError("Research service exceeded the assigned round budget")
            interpreter: InterpretationService | None = self._interpreter
            if result.interpretation_work and interpreter is None:
                raise ValueError("Fetched research evidence requires an explicit interpreter")
            outcomes: tuple[InterpretationOutcome, ...] = (
                tuple(
                    interpreter.interpret(
                        work,
                        run_id=run_id,
                        requested_as_of=requested_as_of,
                        context_known_at=decision_at,
                        baseline_only=False,
                    )
                    for work in result.interpretation_work
                )
                if interpreter is not None
                else ()
            )
        except Exception:
            self._planned_tasks.release(tuple(item[0] for item in materialized))
            raise
        self._planned_tasks.complete(
            tuple(item[0] for item in materialized),
            completed_at=decision_at,
        )
        context = _build_round_context(
            round_number,
            tasks,
            result,
            outcomes,
            decision_at,
            self._material_assessor,
        )
        return ResearchRoundExecution(
            task_count=result.task_count,
            query_count=query_count,
            fetch_count=fetch_count,
            source_item_ids=result.source_item_ids,
            asset_ids=result.asset_ids,
            document_ids=result.document_ids,
            fragment_ids=result.fragment_ids,
            observation_ids=tuple(
                observation.observation_id for outcome in outcomes for observation in outcome.observations
            ),
            interpretation_attempt_ids=tuple(outcome.attempt_id for outcome in outcomes),
            failure_ids=(),
            independent_provenance_groups=result.independent_provenance_groups,
            failure_kinds=result.failure_kinds,
            context=context,
        )

    def finalize_session(
        self,
        session_id: str,
        *,
        run_id: str,
        reason: ResearchStopReason,
        stopped_at: datetime,
        known_at: datetime,
        summary_payload: JsonValue,
    ) -> str:
        """Atomically stop a session and retain its exact recoverable semantics."""
        return self._repository.finalize_session(
            session_id,
            run_id=run_id,
            reason=reason,
            stopped_at=stopped_at,
            known_at=known_at,
            summary_payload=summary_payload,
        )

    def stage_admission(
        self,
        *,
        run_id: str,
        session_ids: tuple[str, ...],
        known_at: datetime,
    ) -> ResearchStageAdmission:
        """Reconstruct exact durable A3 descendants for atomic stage admission."""
        return self._repository.stage_admission(run_id=run_id, session_ids=session_ids, known_at=known_at)

    def recover_stage_admission(
        self,
        *,
        run_id: str,
        known_at: datetime,
    ) -> RecoveredResearchStage | None:
        """Recover exact terminal A3 semantics without provider or model calls."""
        return self._repository.recover_stage_admission(run_id, known_at=known_at)


def _build_round_context(
    round_number: int,
    tasks: tuple[ResearchTaskDraft, ...],
    result: ResearchRoundResult,
    outcomes: tuple[InterpretationOutcome, ...],
    decision_at: datetime,
    assessor: ClaimVerificationMaterialAssessor | None,
) -> ResearchCumulativeContext:
    associations = {item.source_item_id: item for item in result.source_associations}
    if len(associations) != len(result.source_associations):
        raise ValueError("Research result source associations must have unique source item identities")
    evidence_records: list[ResearchEvidenceRecord] = []
    bindings: list[EvidenceAliasBinding] = []
    sequence = 0
    for work in result.interpretation_work:
        association = associations.get(work.document.asset.source_item_id)
        if association is None:
            raise ValueError("Research evidence omitted its exact source association")
        definition = association.source_definition
        trust = next(
            (setting.level for setting in definition.trust_settings if setting.category is TrustCategory.FACTUAL),
            TrustLevel.UNTRUSTED,
        )
        for item in project_evidence(work.document.fragments).evidence:
            sequence += 1
            alias = f"{item.alias[0]}{round_number:02d}{sequence:04d}"
            evidence_records.append(
                ResearchEvidenceRecord(
                    alias=alias,
                    kind=item.kind,
                    text=item.text,
                    provenance_group=definition.provenance_group,
                    trust_level=trust,
                ),
            )
            bindings.append(
                EvidenceAliasBinding(
                    alias=alias,
                    fragment_ids=item.fragment_ids,
                    source_item_id=work.document.asset.source_item_id,
                    provenance_group=definition.provenance_group,
                    allowed_uses=definition.allowed_uses,
                    trust_level=trust,
                ),
            )
    material_keys = tuple(dict.fromkeys(key for task in tasks for key in task.material_claim_keys))
    provenance = tuple(sorted({item.provenance_group for item in bindings}))
    assessment = (
        assessor.assess(
            material_keys,
            bindings=tuple(bindings),
            decision_at=decision_at,
            provisional_targets=result.interpretation_targets,
            outcomes=outcomes,
        )
        if assessor is not None
        else MaterialAnchorAssessment(
            material_claim_keys=material_keys,
            unresolved_claim_keys=material_keys,
            independent_provenance_groups=provenance,
        )
    )
    return ResearchCumulativeContext(
        new_observations=tuple(observation for outcome in outcomes for observation in outcome.observations),
        evidence=tuple(evidence_records),
        alias_bindings=tuple(bindings),
        provenance_groups=provenance,
        failure_kinds=result.failure_kinds,
        material_anchor_assessment=assessment,
    )


def _planned_task_id(run_id: str, candidate_id: str, task_json: str) -> str:
    digest: str = hashlib.sha256(
        f"{run_id}\0{candidate_id}\0{task_json}".encode("utf-8"),
    ).hexdigest()
    return f"planned:{digest}"


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Planned research timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])
