from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from money_pit.contracts import ResearchTaskDraft
from money_pit.evidence.work import EvidenceInterpretationWork
from money_pit.pipeline.interpretation import InterpretationOutcome
from money_pit.pipeline.interpretation import InterpretationService
from money_pit.portfolio.theses import ThesisRepository
from money_pit.research.memory import DurableResearchRoundRunner
from money_pit.research.memory import PlannedResearchTaskStore
from money_pit.research.repository import ResearchBudgetState
from money_pit.research.repository import ResearchRepository
from money_pit.research.service import ResearchRoundResult
from money_pit.research.service import ResearchService
from money_pit.research.service import ResearchSourceAssociation
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.research import CandidateThesisResearchScope
from money_pit.schemas.research import ResearchQuery
from money_pit.schemas.research import ResearchSession
from money_pit.schemas.research import ResearchTask
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.schemas.theses import CandidateStatus
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.sources._shared import source_definition_hash
from money_pit.storage.database import Database


_AS_OF = datetime(2026, 8, 1, tzinfo=UTC)
_RUN_ID = "4fa85f64-5717-4562-b3fc-2c963f66afa6"


def test_candidates_due_for_research_keeps_baseline_unresolved_work_with_new_candidates(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    repository = ThesisRepository(database)
    baseline = CandidateThesis(
        candidate_thesis_id="candidate-baseline",
        subject="Baseline unresolved candidate",
        direction=ThesisDirection.LONG,
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim-baseline",)),
        status=CandidateStatus.UNRESOLVED,
        created_at=_AS_OF - timedelta(days=7),
        known_at=_AS_OF - timedelta(days=7),
    )
    same_run = baseline.model_copy(
        update={
            "candidate_thesis_id": "candidate-new",
            "subject": "New same-run candidate",
            "discovery_basis": DiscoveryBasis(source_claim_keys=("claim-new",)),
            "status": CandidateStatus.OPEN,
            "created_at": _AS_OF + timedelta(minutes=1),
            "known_at": _AS_OF + timedelta(minutes=1),
        },
    )
    repository.append_candidate(baseline)
    repository.append_candidate(same_run)

    due = repository.candidates_due_for_research(
        as_of=_AS_OF,
        exact_candidate_ids=(same_run.candidate_thesis_id,),
    )

    assert tuple(candidate.candidate_thesis_id for candidate in due) == (
        baseline.candidate_thesis_id,
        same_run.candidate_thesis_id,
    )


def test_materialize_binds_an_a2_plan_to_the_bounded_a3_session(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    with database.transaction() as connection:
        _ = connection.execute(
            """
                INSERT INTO runs (
                    run_id, requested_as_of, started_at, known_at, through_stage,
                    source_config_hash, intelligence_config_hash, manifest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _RUN_ID,
                _AS_OF.isoformat(),
                _AS_OF.isoformat(),
                _AS_OF.isoformat(),
                "A3",
                "a" * 64,
                "b" * 64,
                "{}",
            ),
        )
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-new",
        subject="New company margin expansion",
        direction=ThesisDirection.LONG,
        instrument="NEW",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim-new",)),
        created_at=_AS_OF,
        known_at=_AS_OF,
    )
    ThesisRepository(database).append_candidate(candidate)
    session = ResearchSession(
        session_id="research-session-1",
        run_id=_RUN_ID,
        scope=CandidateThesisResearchScope(candidate_thesis_id=candidate.candidate_thesis_id),
        started_at=_AS_OF,
        deadline_at=_AS_OF + timedelta(minutes=10),
        maximum_rounds=3,
        maximum_queries=12,
        maximum_fetches=24,
    )
    _ = ResearchRepository(database).create_session(session)
    planned = PlannedResearchTaskStore(database)
    task = ResearchTaskDraft(
        candidate_thesis_id=candidate.candidate_thesis_id,
        provider="edgar",
        query="NEW quarterly filing gross margin",
        purpose="Verify the material margin anchor",
        maximum_results=4,
    )
    _ = planned.append_task(task, run_id=_RUN_ID, known_at=_AS_OF)

    _, execution_task = planned.materialize(
        task,
        run_id=_RUN_ID,
        session_id=session.session_id,
        round_number=1,
        as_of=_AS_OF,
    )

    assert execution_task.session_id == session.session_id


class _UnusedDependency:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(name)


def _candidate() -> CandidateThesis:
    return CandidateThesis(
        candidate_thesis_id="candidate-1",
        subject="Candidate",
        direction=ThesisDirection.LONG,
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        created_at=_AS_OF,
        known_at=_AS_OF,
    )


def _draft(*, query: str = "query", maximum_results: int = 1) -> ResearchTaskDraft:
    return ResearchTaskDraft(
        candidate_thesis_id="candidate-1",
        provider="brave",
        query=query,
        purpose="verify",
        maximum_results=maximum_results,
    )


@pytest.mark.parametrize(
    ("tasks", "query_budget", "fetch_budget", "expected_message"),
    [
        ((_draft(), _draft(query="other")), 1, 2, "Research tasks exceed the assigned query budget"),
        ((_draft(maximum_results=2),), 1, 1, "Research tasks exceed the assigned fetch budget"),
    ],
)
def test_run_round_rejects_allocations_before_service_or_queue_io(
    tasks: tuple[ResearchTaskDraft, ...],
    query_budget: int,
    fetch_budget: int,
    expected_message: str,
) -> None:
    unused = _UnusedDependency()
    runner = DurableResearchRoundRunner(
        cast("ResearchService", cast("object", unused)),
        cast("ResearchRepository", cast("object", unused)),
        cast("PlannedResearchTaskStore", cast("object", unused)),
    )

    with pytest.raises(ValueError, match=expected_message):
        _ = runner.run_round(
            session_id="session-1",
            run_id=_RUN_ID,
            candidate=_candidate(),
            round_number=1,
            tasks=tasks,
            requested_as_of=_AS_OF,
            decision_at=_AS_OF,
            historical_explicit=False,
            query_budget=query_budget,
            fetch_budget=fetch_budget,
        )


class _PlannedQueue:
    def __init__(self) -> None:
        self.released: tuple[str, ...] = ()
        self.completed: tuple[str, ...] = ()

    def append_task(self, task: ResearchTaskDraft, *, run_id: str, known_at: datetime) -> str:
        del task, run_id, known_at
        return "planned:test"

    def materialize(
        self,
        task: ResearchTaskDraft,
        *,
        run_id: str,
        session_id: str,
        round_number: int,
        as_of: datetime,
    ) -> tuple[str, ResearchTask]:
        del run_id
        return (
            f"planned:{task.query}",
            ResearchTask(
                task_id=f"task:{task.query}",
                session_id=session_id,
                round_number=round_number,
                query=ResearchQuery(
                    provider=task.provider,
                    query_text=task.query,
                    candidate_thesis_id=task.candidate_thesis_id,
                    purpose=task.purpose,
                    requested_at=as_of,
                    max_results=task.maximum_results,
                ),
                created_at=as_of,
            ),
        )

    def release(self, planned_ids: tuple[str, ...]) -> None:
        self.released = planned_ids

    def complete(self, planned_ids: tuple[str, ...], *, completed_at: datetime) -> None:
        del completed_at
        self.completed = planned_ids


class _BudgetRepository:
    def __init__(self) -> None:
        self.session: ResearchSession = ResearchSession(
            session_id="session-1",
            run_id=_RUN_ID,
            scope=CandidateThesisResearchScope(candidate_thesis_id="candidate-1"),
            started_at=_AS_OF,
            deadline_at=_AS_OF + timedelta(hours=1),
        )

    def budget_state(self, session_id: str) -> ResearchBudgetState:
        assert session_id == self.session.session_id
        return ResearchBudgetState(session=self.session, query_count=0, fetch_count=0)


def _work() -> EvidenceInterpretationWork:
    return EvidenceInterpretationWork(
        document=EvidenceDocument(
            asset=EvidenceAsset(
                asset_id="a" * 64,
                content_hash="a" * 64,
                media_type="text/plain",
                source_item_id="source:item",
                local_path=Path("aa") / ("a" * 64),
                retrieved_at=_AS_OF,
            ),
            fragments=(),
        ),
        content_version="version-1",
    )


class _RoundService:
    def __init__(self, *, fail: bool = False, include_work: bool = False) -> None:
        self.fail: bool = fail
        self.include_work: bool = include_work

    def run_round(
        self, session: ResearchSession, tasks: tuple[ResearchTask, ...], **kwargs: object
    ) -> ResearchRoundResult:
        del kwargs
        if self.fail:
            raise RuntimeError("service failed")
        definition = SourceDefinition(
            source_id="publisher-policy",
            adapter_name="research",
            locator="https://example.test",
            provenance_group="publisher",
            allowed_uses=(AllowedUse.FACTUAL_VERIFICATION,),
            trust_settings=(
                SourceTrustSetting(
                    category=TrustCategory.FACTUAL,
                    level=TrustLevel.INDEPENDENT_SECONDARY,
                ),
            ),
        )
        return ResearchRoundResult(
            session_id=session.session_id,
            round_number=tasks[0].round_number,
            task_count=len(tasks),
            query_count=0,
            fetch_count=0,
            source_item_ids=(),
            asset_ids=(),
            independent_provenance_groups=(),
            failure_kinds=(),
            interpretation_work=(_work(),) if self.include_work else (),
            source_associations=(
                ResearchSourceAssociation(
                    source_item_id="source:item",
                    source_definition_hash=source_definition_hash(definition),
                    source_definition=definition,
                ),
            )
            if self.include_work
            else (),
        )


class _RecordingInterpreter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail: bool = fail
        self.baseline_only: list[bool] = []

    def interpret(self, work: EvidenceInterpretationWork, **kwargs: object) -> InterpretationOutcome:
        baseline_only = cast("bool", kwargs.get("baseline_only", False))
        self.baseline_only.append(baseline_only)
        if self.fail:
            raise RuntimeError("interpretation failed")
        return InterpretationOutcome(
            attempt_id="attempt-1",
            document_id=work.document.asset.asset_id,
            fragment_ids=(),
            observations=(),
            requests=(),
            responses=(),
            alias_maps=(),
            known_at=_AS_OF,
            completed_at=_AS_OF,
        )


def _run_with_collaborators(
    service: _RoundService,
    queue: _PlannedQueue,
    interpreter: _RecordingInterpreter | None,
) -> None:
    repository = _BudgetRepository()
    runner = DurableResearchRoundRunner(
        cast("ResearchService", cast("object", service)),
        cast("ResearchRepository", cast("object", repository)),
        cast("PlannedResearchTaskStore", cast("object", queue)),
        cast("InterpretationService | None", cast("object", interpreter)),
    )
    _ = runner.run_round(
        session_id=repository.session.session_id,
        run_id=_RUN_ID,
        candidate=_candidate(),
        round_number=1,
        tasks=(_draft(),),
        requested_as_of=_AS_OF,
        decision_at=_AS_OF,
        historical_explicit=False,
        query_budget=1,
        fetch_budget=1,
    )


@pytest.mark.parametrize("failure_stage", ["service", "interpreter"])
def test_run_round_releases_materialized_tasks_after_downstream_failure(failure_stage: str) -> None:
    queue = _PlannedQueue()
    service = _RoundService(fail=failure_stage == "service", include_work=failure_stage == "interpreter")
    interpreter = _RecordingInterpreter(fail=True) if failure_stage == "interpreter" else None

    with pytest.raises(RuntimeError):
        _run_with_collaborators(service, queue, interpreter)

    assert queue.released == ("planned:query",)
    assert queue.completed == ()


def test_run_round_interprets_normal_a3_evidence_with_full_baseline_context() -> None:
    queue = _PlannedQueue()
    interpreter = _RecordingInterpreter()

    _run_with_collaborators(_RoundService(include_work=True), queue, interpreter)

    assert interpreter.baseline_only == [False]
    assert queue.completed == ("planned:query",)
