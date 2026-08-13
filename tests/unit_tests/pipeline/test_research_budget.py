from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import cast

import pytest
from pydantic import JsonValue
from typing_extensions import override

from money_pit.agents.inference import InferenceResult
from money_pit.agents.inference import InferenceUsage
from money_pit.contracts import ResearchPlanningRequest
from money_pit.contracts import ResearchRoundExecution
from money_pit.contracts import ResearchRoundPlan
from money_pit.contracts import ResearchRoundRunner
from money_pit.contracts import ResearchTaskDraft
from money_pit.contracts import ResearchTaskMemory
from money_pit.pipeline.research import CandidateResearchSummary
from money_pit.pipeline.research import ResearchBudget
from money_pit.pipeline.research import _select_research_tasks  # pyright: ignore[reportPrivateUsage]
from money_pit.pipeline.research import research_candidate
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import RecoveredResearchStage
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.research import ResearchStageAdmission
from money_pit.schemas.research import ResearchStopReason
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.storage.intelligence_work import ResearchCheckpointRecord


_NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


class _CrashResumeRunner:
    def __init__(self) -> None:
        self.round_calls: int = 0
        self.finalize_calls: int = 0

    def resume_or_start_session(self, **_kwargs: object) -> str:
        return "session-1"

    def pending_tasks_for_session(self, session_id: str) -> tuple[ResearchTaskDraft, ...]:
        del session_id
        return ()

    def run_round(self, **_kwargs: object) -> ResearchRoundExecution:
        self.round_calls += 1
        execution = ResearchRoundExecution(
            task_count=1,
            query_count=1,
            fetch_count=1,
            independent_provenance_groups=("issuer-primary",),
        )
        callback = cast("object", _kwargs.get("on_completed"))
        if callable(callback):
            _ = callback(execution)
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
        del session_id, run_id, reason, stopped_at, known_at, summary_payload
        self.finalize_calls += 1
        return "summary-1"

    def start_session(self, **_kwargs: object) -> str:
        raise AssertionError("The incremental path must resume or start explicitly.")

    def stage_admission(self, **_kwargs: object) -> ResearchStageAdmission:
        raise AssertionError("This non-terminal wave must not be admitted.")

    def recover_stage_admission(self, **_kwargs: object) -> RecoveredResearchStage | None:
        return None


class _NoopTaskMemory:
    def __init__(self) -> None:
        self._plans: dict[tuple[str, int], ResearchRoundPlan] = {}

    def append_task(
        self,
        task: ResearchTaskDraft,
        *,
        run_id: str,
        known_at: datetime,
        origin_unit_ids: tuple[str, ...] = (),
    ) -> str:
        del task, run_id, known_at, origin_unit_ids
        return "task-1"

    def checkpoint_planner_tasks(
        self,
        tasks: tuple[ResearchTaskDraft, ...],
        *,
        run_id: str,
        known_at: datetime,
    ) -> tuple[str, ...]:
        del run_id, known_at
        return tuple(f"task-{index}" for index, _task in enumerate(tasks))

    def planner_result(self, *, job_id: str, wave_number: int) -> ResearchRoundPlan | None:
        return self._plans.get((job_id, wave_number))

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
        del run_id, known_at, request
        self._plans[(job_id, wave_number)] = result

    def pending_for_candidate(
        self,
        candidate_thesis_id: str,
        *,
        run_id: str,
        as_of: datetime,
        origin_unit_ids: tuple[str, ...] = (),
    ) -> tuple[ResearchTaskDraft, ...]:
        del candidate_thesis_id, run_id, as_of, origin_unit_ids
        return ()


class _CrashAfterPlannerCheckpointMemory(_NoopTaskMemory):
    def __init__(self) -> None:
        super().__init__()
        self._crash: bool = True

    @override
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
        super().checkpoint_planner_result(
            job_id=job_id,
            wave_number=wave_number,
            run_id=run_id,
            known_at=known_at,
            request=request,
            result=result,
        )
        if self._crash:
            self._crash = False
            raise RuntimeError("simulated hard kill after planner checkpoint")


def _candidate() -> CandidateThesis:
    return CandidateThesis(
        candidate_thesis_id="candidate-1",
        subject="Issuer backlog conversion",
        direction=ThesisDirection.LONG,
        instrument_reference="ISSUER",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        created_at=_NOW,
        known_at=_NOW,
    )


def _task(index: int, *, maximum_results: int) -> ResearchTaskDraft:
    return ResearchTaskDraft(
        candidate_thesis_id="candidate-1",
        provider="brave",
        query=f"query-{index}",
        purpose=f"purpose-{index}",
        maximum_results=maximum_results,
    )


def test__select_research_tasks_preserves_original_task_identity() -> None:
    tasks = tuple(_task(index, maximum_results=maximum) for index, maximum in enumerate((5, 5, 5, 5, 20)))

    selected = _select_research_tasks(tasks, query_budget=10, fetch_budget=19)

    assert selected == tasks


def test__select_research_tasks_limits_task_count_by_query_budget() -> None:
    tasks = tuple(_task(index, maximum_results=5) for index in range(5))

    selected = _select_research_tasks(tasks, query_budget=3, fetch_budget=19)

    assert tuple(task.query for task in selected) == ("query-0", "query-1", "query-2")


def test__select_research_tasks_limits_task_count_by_minimum_fetch_coverage() -> None:
    tasks = tuple(_task(index, maximum_results=20) for index in range(4))

    selected = _select_research_tasks(tasks, query_budget=4, fetch_budget=2)

    assert tuple(task.query for task in selected) == ("query-0", "query-1")
    assert tuple(task.maximum_results for task in selected) == (20, 20)


@pytest.mark.parametrize(("query_budget", "fetch_budget"), [(0, 1), (1, 0)])
def test__select_research_tasks_returns_empty_with_exhausted_budget(
    query_budget: int,
    fetch_budget: int,
) -> None:
    tasks = (_task(0, maximum_results=5),)

    assert _select_research_tasks(tasks, query_budget=query_budget, fetch_budget=fetch_budget) == ()


def test_research_candidate_resumes_after_crash_without_repeating_durable_wave() -> None:
    runner = _CrashResumeRunner()
    checkpoints: list[ResearchCheckpointRecord] = []
    planner_calls = 0
    task = ResearchTaskDraft(
        candidate_thesis_id="candidate-1",
        provider="edgar",
        query="Issuer quarterly backlog",
        purpose="Verify backlog conversion.",
        material_claim_keys=("claim-1",),
        maximum_results=1,
    )

    def planner(_request: ResearchPlanningRequest) -> InferenceResult[ResearchRoundPlan]:
        nonlocal planner_calls
        planner_calls += 1
        return InferenceResult(
            output=ResearchRoundPlan(stop_reason=ResearchStopReason.UNRESOLVED),
            usage=InferenceUsage(),
            request_hash="a" * 64,
        )

    def persist_then_crash(value: ResearchCheckpointRecord) -> None:
        checkpoints.append(value)
        raise RuntimeError("crash after durable wave")

    with pytest.raises(RuntimeError):
        _ = research_candidate(
            _candidate(),
            run_id="run-1",
            job_id="job-1",
            requested_as_of=_NOW,
            historical_explicit=False,
            initial_tasks=(task,),
            runner=cast("ResearchRoundRunner", runner),
            planner=planner,
            task_memory=cast("ResearchTaskMemory", _NoopTaskMemory()),
            budget=ResearchBudget(maximum_elapsed=timedelta(minutes=10)),
            clock=lambda: _NOW,
            prompt_character_budget=10_000,
            allowed_provider_names=("edgar",),
            checkpoint_wave=persist_then_crash,
        )

    assert len(checkpoints) == 1
    _ = research_candidate(
        _candidate(),
        run_id="run-2",
        job_id="job-1",
        requested_as_of=_NOW,
        historical_explicit=False,
        initial_tasks=(task,),
        runner=cast("ResearchRoundRunner", runner),
        planner=planner,
        task_memory=cast("ResearchTaskMemory", _NoopTaskMemory()),
        budget=ResearchBudget(maximum_elapsed=timedelta(minutes=10)),
        clock=lambda: _NOW,
        prompt_character_budget=10_000,
        allowed_provider_names=("edgar",),
        prior_checkpoint=checkpoints[0],
    )

    assert (runner.round_calls, runner.finalize_calls, planner_calls) == (1, 1, 0)


def test_research_candidate_resumes_empty_planner_result_without_repeating_inference() -> None:
    runner = _CrashResumeRunner()
    memory = _CrashAfterPlannerCheckpointMemory()
    planner_calls = 0

    def planner(_request: ResearchPlanningRequest) -> InferenceResult[ResearchRoundPlan]:
        nonlocal planner_calls
        planner_calls += 1
        return InferenceResult(
            output=ResearchRoundPlan(stop_reason=ResearchStopReason.UNRESOLVED),
            usage=InferenceUsage(),
            request_hash="b" * 64,
        )

    def invoke(run_id: str) -> CandidateResearchSummary:
        return research_candidate(
            _candidate(),
            run_id=run_id,
            job_id="job-1",
            requested_as_of=_NOW,
            historical_explicit=False,
            initial_tasks=(),
            runner=cast("ResearchRoundRunner", runner),
            planner=planner,
            task_memory=cast("ResearchTaskMemory", memory),
            budget=ResearchBudget(maximum_elapsed=timedelta(minutes=10)),
            clock=lambda: _NOW,
            prompt_character_budget=10_000,
            allowed_provider_names=("edgar",),
        )

    with pytest.raises(RuntimeError, match="planner checkpoint"):
        _ = invoke("run-1")

    result = invoke("run-2")

    assert planner_calls == 1
    assert result.stop_reason is ResearchStopReason.NO_NEW_INDEPENDENT_PROVENANCE


@pytest.mark.parametrize(
    ("assessment", "expected_reason"),
    [
        (
            MaterialAnchorAssessment(
                material_claim_keys=("claim-1",),
                supported_claim_keys=("claim-1",),
                evidence_standard_satisfied=True,
            ),
            ResearchStopReason.EVIDENCE_STANDARD_SATISFIED,
        ),
        (
            MaterialAnchorAssessment(
                material_claim_keys=("claim-1",),
                contradicted_claim_keys=("claim-1",),
                decisive_contradiction=True,
            ),
            ResearchStopReason.DECISIVE_CONTRADICTION,
        ),
    ],
)
def test_research_candidate_stops_from_terminal_checkpoint_before_planner(
    assessment: MaterialAnchorAssessment,
    expected_reason: ResearchStopReason,
) -> None:
    runner = _CrashResumeRunner()
    context = ResearchCumulativeContext(material_anchor_assessment=assessment)
    prior_summary = CandidateResearchSummary(
        candidate_thesis_id="candidate-1",
        session_id="session-1",
        rounds=(),
        stop_reason=ResearchStopReason.UNRESOLVED,
    )
    checkpoint = ResearchCheckpointRecord(
        checkpoint_id="terminal-checkpoint",
        job_id="job-1",
        run_id="run-1",
        wave_number=1,
        search_count=1,
        accepted_fetch_count=1,
        recorded_at=_NOW,
        digest={
            "candidate": prior_summary.model_dump(mode="json"),
            "contexts": [context.model_dump(mode="json")],
        },
    )

    def reject_planner(_request: ResearchPlanningRequest) -> InferenceResult[ResearchRoundPlan]:
        raise AssertionError("terminal checkpoint must stop before planning")

    result = research_candidate(
        _candidate(),
        run_id="run-2",
        job_id="job-1",
        requested_as_of=_NOW,
        historical_explicit=False,
        initial_tasks=(),
        runner=cast("ResearchRoundRunner", runner),
        planner=reject_planner,
        task_memory=cast("ResearchTaskMemory", _NoopTaskMemory()),
        budget=ResearchBudget(maximum_elapsed=timedelta(minutes=10)),
        clock=lambda: _NOW,
        prompt_character_budget=10_000,
        allowed_provider_names=("edgar",),
        prior_checkpoint=checkpoint,
    )

    assert (result.stop_reason, runner.round_calls, runner.finalize_calls) == (
        expected_reason,
        0,
        1,
    )
