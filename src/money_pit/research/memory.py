"""Module adapting planned research work to bounded execution sessions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import TYPE_CHECKING
from typing import cast

from money_pit.contracts import ResearchPlanningRequest
from money_pit.contracts import ResearchRoundExecution
from money_pit.contracts import ResearchRoundPlan
from money_pit.contracts import ResearchTaskDraft
from money_pit.evidence.aliases import project_evidence
from money_pit.research.errors import ResearchBudgetExceededError
from money_pit.research.service import ResearchRoundResult
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
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import ProviderResearchWaveRecord


if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from pydantic import JsonValue

    from money_pit.pipeline.interpretation import InterpretationOutcome
    from money_pit.pipeline.interpretation import InterpretationService
    from money_pit.research.assessment import ClaimVerificationMaterialAssessor
    from money_pit.research.repository import ResearchRepository
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
        origin_unit_ids: tuple[str, ...] = (),
    ) -> str:
        """Append one immutable candidate plan with durable discovery origins."""
        candidate_id: str | None = task.candidate_thesis_id
        if candidate_id is None:
            raise ValueError("Planned research task must identify a candidate")
        task_json: str = task.model_dump_json()
        task_id: str = _planned_task_id(candidate_id, task_json)
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
                if row is None or str(_column(row, "task_json")) != task_json:
                    raise ValueError("Planned research task identity collision")
            for unit_id in origin_unit_ids:
                _ = connection.execute(
                    """INSERT INTO planned_research_task_origins (task_id, unit_id)
                    VALUES (?, ?) ON CONFLICT DO NOTHING""",
                    (task_id, unit_id),
                )
        return task_id

    def checkpoint_planner_tasks(
        self,
        tasks: tuple[ResearchTaskDraft, ...],
        *,
        run_id: str,
        known_at: datetime,
    ) -> tuple[str, ...]:
        """Atomically persist one paid planner result before execution."""
        prepared: list[tuple[str, str, str]] = []
        for task in tasks:
            candidate_id = task.candidate_thesis_id
            if candidate_id is None:
                raise ValueError("Planned research task must identify a candidate")
            task_json = task.model_dump_json()
            prepared.append((_planned_task_id(candidate_id, task_json), candidate_id, task_json))
        with self._database.transaction(TransactionMode.WRITE) as connection:
            for task_id, candidate_id, task_json in prepared:
                _ = connection.execute(
                    """INSERT INTO planned_research_tasks (
                    task_id, candidate_thesis_id, status, created_at, known_at, run_id, task_json
                    ) VALUES (?, ?, 'pending', ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                    (task_id, candidate_id, _utc_text(known_at), _utc_text(known_at), run_id, task_json),
                )
                row: sqlite3.Row | None = cast(
                    "sqlite3.Row | None",
                    connection.execute(
                        "SELECT task_json FROM planned_research_tasks WHERE task_id = ?",
                        (task_id,),
                    ).fetchone(),
                )
                if row is None or str(_column(row, "task_json")) != task_json:
                    raise ValueError("Planned research task identity collision")
        return tuple(item[0] for item in prepared)

    def planner_result(self, *, job_id: str, wave_number: int) -> ResearchRoundPlan | None:
        """Return a complete durable planner result, including an empty result."""
        with self._database.transaction() as connection:
            row = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT result_json FROM research_planner_results WHERE job_id = ? AND wave_number = ?",
                    (job_id, wave_number),
                ).fetchone(),
            )
        return None if row is None else ResearchRoundPlan.model_validate_json(str(_column(row, "result_json")))

    def checkpoint_planner_result(
        self,
        *,
        job_id: str,
        wave_number: int,
        run_id: str,
        known_at: datetime,
        request: ResearchPlanningRequest,
        result: ResearchRoundPlan,
    ) -> None:
        """Persist a validated paid planner response before task materialization."""
        request_json = request.model_dump_json()
        result_json = result.model_dump_json()
        request_hash = hashlib.sha256(request_json.encode()).hexdigest()
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """INSERT INTO research_planner_results
                (job_id, wave_number, run_id, request_hash, recorded_at, request_json, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                (job_id, wave_number, run_id, request_hash, _utc_text(known_at), request_json, result_json),
            )
            row = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """SELECT request_hash, request_json, result_json FROM research_planner_results
                    WHERE job_id = ? AND wave_number = ?""",
                    (job_id, wave_number),
                ).fetchone(),
            )
            if row is None or tuple(row) != (request_hash, request_json, result_json):
                raise ValueError("Research planner result identity collision")

    def pending_for_candidate(
        self,
        candidate_thesis_id: str,
        *,
        run_id: str,
        as_of: datetime,
        origin_unit_ids: tuple[str, ...] = (),
    ) -> tuple[ResearchTaskDraft, ...]:
        """Return point-in-time pending plans for one candidate across producing runs."""
        del run_id
        with self._database.transaction() as connection:
            if origin_unit_ids:
                rows: list[sqlite3.Row] = cast(
                    "list[sqlite3.Row]",
                    connection.execute(
                        """SELECT DISTINCT task.task_json, task.known_at, task.created_at, task.task_id
                    FROM planned_research_tasks AS task
                    JOIN planned_research_task_origins AS origin ON origin.task_id = task.task_id
                    WHERE task.candidate_thesis_id = ? AND task.status = 'pending'
                      AND task.known_at <= ?
                      AND origin.unit_id IN (SELECT value FROM json_each(?))
                    ORDER BY task.known_at, task.created_at, task.task_id""",
                        (candidate_thesis_id, _utc_text(as_of), json.dumps(origin_unit_ids)),
                    ).fetchall(),
                )
            else:
                rows = cast(
                    "list[sqlite3.Row]",
                    connection.execute(
                        """SELECT task_json FROM planned_research_tasks
                    WHERE candidate_thesis_id = ? AND status = 'pending' AND known_at <= ?
                    ORDER BY known_at, created_at, task_id""",
                        (candidate_thesis_id, _utc_text(as_of)),
                    ).fetchall(),
                )
        return tuple(ResearchTaskDraft.model_validate_json(str(_column(row, "task_json"))) for row in rows)

    def materialize(
        self,
        task: ResearchTaskDraft,
        *,
        allocated_maximum_results: int,
        run_id: str,
        session_id: str,
        round_number: int,
        as_of: datetime,
    ) -> tuple[str, ResearchTask]:
        """Bind one plan to a real session and return its execution contract."""
        del run_id
        candidate_id: str | None = task.candidate_thesis_id
        if candidate_id is None:
            raise ValueError("Research task must identify a candidate")
        if allocated_maximum_results < 1 or allocated_maximum_results > task.maximum_results:
            raise ValueError("Allocated result limit must be within the planned task limit")
        task_json: str = task.model_dump_json()
        planned_id: str = _planned_task_id(candidate_id, task_json)
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
                max_results=allocated_maximum_results,
            ),
            created_at=as_of,
        )
        with self._database.transaction(TransactionMode.WRITE) as connection:
            planned_row = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """SELECT task_id FROM planned_research_tasks
                WHERE candidate_thesis_id = ? AND task_json = ? AND status = 'pending'
                ORDER BY known_at, created_at, task_id LIMIT 1""",
                    (candidate_id, task_json),
                ).fetchone(),
            )
            if planned_row is None:
                raise KeyError(_planned_task_id(candidate_id, task_json))
            planned_id = str(_column(planned_row, "task_id"))
            execution_task = execution_task.model_copy(update={"task_id": f"execution:{planned_id}:{round_number}"})
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

    def append_wave_result(
        self,
        *,
        job_id: str,
        session_id: str,
        run_id: str,
        wave_number: int,
        recorded_at: datetime,
        execution: ResearchRoundExecution,
    ) -> None:
        """Append the validated wave outbox before work-ledger reconciliation."""
        wave_result_id = hashlib.sha256(f"{job_id}\0{wave_number}".encode("utf-8")).hexdigest()
        context_payload = None
        if execution.context is not None:
            context_payload = execution.context.model_dump(mode="json")
            context_payload["alias_bindings"] = [
                binding.model_dump(mode="json") for binding in execution.context.alias_bindings
            ]
        accepted_fetch_count = self._accepted_fetch_delta(job_id)
        encoded = json.dumps(
            {
                "execution": execution.model_dump(mode="json"),
                "context": context_payload,
                "search_count_delta": execution.query_count,
                "accepted_fetch_count_delta": accepted_fetch_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._database.transaction(TransactionMode.WRITE) as connection:
            row = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT phase FROM research_wave_results WHERE wave_result_id = ?",
                    (wave_result_id,),
                ).fetchone(),
            )
            if row is None:
                _ = connection.execute(
                    """INSERT INTO research_wave_results
                    (wave_result_id, job_id, session_id, run_id, wave_number, phase, recorded_at,
                     provider_result_json, execution_json)
                    VALUES (?, ?, ?, ?, ?, 'execution_completed', ?, 'null', ?)""",
                    (wave_result_id, job_id, session_id, run_id, wave_number, _utc_text(recorded_at), encoded),
                )
            elif str(_column(row, "phase")) == "provider_completed":
                _ = connection.execute(
                    """UPDATE research_wave_results SET phase = 'execution_completed', execution_json = ?
                    WHERE wave_result_id = ? AND phase = 'provider_completed'""",
                    (encoded, wave_result_id),
                )
            stored = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """SELECT job_id, session_id, wave_number, execution_json
                FROM research_wave_results WHERE wave_result_id = ?""",
                    (wave_result_id,),
                ).fetchone(),
            )
            if stored is None or tuple(stored) != (job_id, session_id, wave_number, encoded):
                raise ValueError("Research wave result identity collision")

    def append_provider_wave_result(
        self,
        *,
        job_id: str,
        session_id: str,
        run_id: str,
        wave_number: int,
        recorded_at: datetime,
        result: ResearchRoundResult,
    ) -> None:
        """Append provider-complete semantics before fallible interpretations."""
        wave_result_id = hashlib.sha256(f"{job_id}\0{wave_number}".encode("utf-8")).hexdigest()
        encoded_result = result.model_dump_json()
        search_count = self._search_count_delta(job_id, session_id)
        accepted_fetch_count = self._accepted_fetch_delta(job_id)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """INSERT OR IGNORE INTO research_wave_results
                (wave_result_id, job_id, session_id, run_id, wave_number, phase, recorded_at,
                 provider_result_json, execution_json)
                VALUES (?, ?, ?, ?, ?, 'provider_completed', ?, ?, ?)""",
                (
                    wave_result_id,
                    job_id,
                    session_id,
                    run_id,
                    wave_number,
                    _utc_text(recorded_at),
                    encoded_result,
                    json.dumps(
                        {
                            "search_count_delta": search_count,
                            "accepted_fetch_count_delta": accepted_fetch_count,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )

    def _search_count_delta(self, job_id: str, session_id: str) -> int:
        """Return exact durable query reservations for this one-wave session."""
        del job_id
        with self._database.transaction() as connection:
            row = cast(
                "sqlite3.Row",
                connection.execute(
                    "SELECT query_count AS reserved FROM research_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone(),
            )
        return int(str(_column(row, "reserved")))

    def _accepted_fetch_delta(self, job_id: str) -> int:
        """Count newly accepted URI admissions beyond the last durable checkpoint."""
        with self._database.transaction() as connection:
            row = cast(
                "sqlite3.Row",
                connection.execute(
                    """SELECT
                    (SELECT count(*) FROM research_uri_admissions
                     WHERE job_id = ? AND consumes_fetch_capacity = 1) AS admitted,
                    COALESCE((SELECT accepted_fetch_count FROM research_job_checkpoints
                              WHERE job_id = ? ORDER BY wave_number DESC LIMIT 1), 0) AS checkpointed""",
                    (job_id, job_id),
                ).fetchone(),
            )
        admitted = int(str(_column(row, "admitted")))
        checkpointed = int(str(_column(row, "checkpointed")))
        if admitted < checkpointed:
            raise ValueError("Accepted URI admission count precedes its durable checkpoint")
        return admitted - checkpointed

    def pending_provider_wave_result(self, job_id: str) -> ProviderResearchWaveRecord | None:
        """Return provider-complete work that still needs semantic interpretation."""
        return IntelligenceWorkRepository(self._database).pending_provider_wave(job_id)


def _allocate_result_limits(
    tasks: tuple[ResearchTaskDraft, ...],
    *,
    fetch_budget: int,
) -> tuple[int, ...]:
    """Reserve one result per task before adding depth in task order."""
    if len(tasks) > fetch_budget:
        raise ResearchBudgetExceededError("Research tasks exceed the assigned fetch budget")
    remaining_fetches: int = fetch_budget - len(tasks)
    allocated: list[int] = []
    for task in tasks:
        additional_results: int = min(task.maximum_results - 1, remaining_fetches)
        allocated.append(1 + additional_results)
        remaining_fetches -= additional_results
    return tuple(allocated)


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

    def resume_or_start_session(
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
        """Resume the candidate's interrupted session without repaying completed provider work."""
        scope = CandidateThesisResearchScope(candidate_thesis_id=candidate.candidate_thesis_id)
        active = self._repository.active_session_for_scope(scope)
        if active is not None:
            if active.run_id != run_id:
                active = self._repository.adopt_interrupted_session(
                    active,
                    run_id=run_id,
                    reclaim_before=started_at - timedelta(minutes=30),
                    deadline=deadline,
                )
            return active.session_id
        return self.start_session(
            run_id=run_id,
            candidate=candidate,
            started_at=started_at,
            deadline=deadline,
            maximum_rounds=maximum_rounds,
            maximum_queries=maximum_queries,
            maximum_fetches=maximum_fetches,
        )

    def pending_tasks_for_session(self, session_id: str) -> tuple[ResearchTaskDraft, ...]:
        """Project unfinished durable execution tasks back to their typed plan shape."""
        latest_round = self._repository.latest_research_round_state(session_id)
        durable_tasks = () if latest_round is None else latest_round.tasks
        return tuple(
            ResearchTaskDraft(
                candidate_thesis_id=task.query.candidate_thesis_id,
                provider=task.query.provider,
                query=task.query.query_text,
                purpose=task.query.purpose,
                material_claim_keys=task.query.material_claim_keys,
                maximum_results=task.query.max_results,
            )
            for task in (durable_tasks or self._repository.pending_tasks(session_id))
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

    def run_round(  # noqa: C901 - explicit durable phase recovery branches
        self,
        *,
        session_id: str,
        job_id: str,
        run_id: str,
        candidate: CandidateThesis,
        round_number: int,
        tasks: tuple[ResearchTaskDraft, ...],
        requested_as_of: datetime,
        decision_at: datetime,
        historical_explicit: bool,
        query_budget: int,
        fetch_budget: int,
        on_completed: Callable[[ResearchRoundExecution], None] | None = None,
    ) -> ResearchRoundExecution:
        """Materialize plans, execute providers, and complete durable queue rows."""
        del candidate
        if len(tasks) > query_budget:
            raise ResearchBudgetExceededError("Research tasks exceed the assigned query budget")
        if len(tasks) > fetch_budget:
            raise ResearchBudgetExceededError("Research tasks exceed the assigned fetch budget")
        pending_provider = (
            None if job_id.startswith("legacy:") else self._planned_tasks.pending_provider_wave_result(job_id)
        )
        durable_round = self._repository.research_round_state(session_id, round_number)
        resumed_execution_tasks = durable_round.tasks
        allocated_result_limits: tuple[int, ...] = _allocate_result_limits(tasks, fetch_budget=fetch_budget)
        for task in tasks:
            _ = self._planned_tasks.append_task(task, run_id=run_id, known_at=decision_at)
        materialized = (
            ()
            if pending_provider is not None or resumed_execution_tasks
            else tuple(
                self._planned_tasks.materialize(
                    task,
                    allocated_maximum_results=allocated_result_limit,
                    run_id=run_id,
                    session_id=session_id,
                    round_number=round_number,
                    as_of=decision_at,
                )
                for task, allocated_result_limit in zip(tasks, allocated_result_limits, strict=True)
            )
        )
        provider_phase_durable = pending_provider is not None
        try:
            before = self._repository.budget_state(session_id)
            result = (
                ResearchRoundResult.model_validate(pending_provider.provider_result)
                if pending_provider is not None
                else self._service.run_round(
                    before.session,
                    resumed_execution_tasks or tuple(item[1] for item in materialized),
                    requested_as_of=requested_as_of,
                    decision_at=decision_at,
                    historical_explicit=historical_explicit,
                    job_id=job_id,
                )
            )
            if pending_provider is None and not job_id.startswith("legacy:"):
                self._planned_tasks.append_provider_wave_result(
                    job_id=job_id,
                    session_id=session_id,
                    run_id=run_id,
                    wave_number=round_number,
                    recorded_at=decision_at,
                    result=result,
                )
                provider_phase_durable = True
                pending_provider = self._planned_tasks.pending_provider_wave_result(job_id)
                if pending_provider is None:
                    raise ValueError("Durable provider wave could not be reloaded")
            after = self._repository.budget_state(session_id)
            query_count = (
                pending_provider.search_count_delta
                if pending_provider is not None
                else after.query_count - before.query_count
            )
            fetch_count = (
                pending_provider.accepted_fetch_count_delta
                if pending_provider is not None
                else after.fetch_count - before.fetch_count
            )
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
            if not provider_phase_durable:
                self._planned_tasks.release(tuple(item[0] for item in materialized))
            raise
        if materialized:
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
        execution = ResearchRoundExecution(
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
        if not job_id.startswith("legacy:"):
            self._planned_tasks.append_wave_result(
                job_id=job_id,
                session_id=session_id,
                run_id=run_id,
                wave_number=round_number,
                recorded_at=decision_at,
                execution=execution,
            )
        if on_completed is not None:
            on_completed(execution)
        return execution

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


def _planned_task_id(candidate_id: str, task_json: str) -> str:
    digest: str = hashlib.sha256(
        f"{candidate_id}\0{task_json}".encode("utf-8"),
    ).hexdigest()
    return f"planned:{digest}"


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Planned research timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])
