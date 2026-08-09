"""Module implementing the bounded iterative A3 research loop."""

from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue

from money_pit.agents.budget import request_character_allowance
from money_pit.agents.budget import serialized_inference_request_size
from money_pit.contracts import ResearchPlanningAgent
from money_pit.contracts import ResearchPlanningRequest
from money_pit.contracts import ResearchRoundExecution
from money_pit.contracts import ResearchRoundPlan
from money_pit.contracts import ResearchRoundRunner
from money_pit.contracts import ResearchTaskDraft
from money_pit.contracts import ResearchTaskMemory
from money_pit.contracts import ThesisMemory
from money_pit.graph.edges import require_predecessor
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import completed_with
from money_pit.graph.state import require_requested_as_of
from money_pit.graph.state import require_run_dir
from money_pit.graph.state import require_run_id
from money_pit.graph.state import require_run_started_at
from money_pit.pipeline.artifacts import build_stage_artifact
from money_pit.pipeline.artifacts import try_install_stage_artifact_file
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.discovery import UnknownResearchProviderError
from money_pit.pipeline.temporal import require_model_temporal_authority
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.research import EvidenceAliasBinding
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import ProvisionalAnchorEvidence
from money_pit.schemas.research import RecoveredResearchStage
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.research import ResearchEvidenceRecord
from money_pit.schemas.research import ResearchStopReason
from money_pit.schemas.theses import CandidateThesis
from money_pit.storage.admission import IntelligenceAdmissionRepository


class ResearchBudgetViolationError(Exception):
    """Raised when a research adapter exceeds the budget assigned to it."""


class InvalidResearchStopReasonError(Exception):
    """Raised when an agent stop reason contradicts deterministic context."""


class PromptProjectionError(Exception):
    """Raised when one atomic model-visible record cannot fit the configured budget."""


class ResearchAliasProjectionError(Exception):
    """Raised when cumulative research evidence cannot retain exact alias bindings."""


class ResearchBudget(BaseModel):
    """Hard per-candidate A3 limits."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    maximum_rounds: int = Field(default=3, ge=1)
    maximum_queries: int = Field(default=12, ge=1)
    maximum_fetches: int = Field(default=24, ge=1)
    maximum_elapsed: timedelta = Field(default=timedelta(minutes=10), gt=timedelta(0))


class CandidateResearchSummary(BaseModel):
    """Immutable A3 summary for one candidate."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    candidate_thesis_id: str
    session_id: str
    rounds: tuple[ResearchRoundExecution, ...]
    stop_reason: ResearchStopReason
    planner_requests: tuple[ResearchPlanningRequest, ...] = ()
    planner_responses: tuple[ResearchRoundPlan, ...] = ()


class ResearchArtifactPayload(BaseModel):
    """Immutable A3 audit payload."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[CandidateResearchSummary, ...]
    contexts: tuple[ResearchCumulativeContext, ...]
    alias_bindings: tuple[EvidenceAliasBinding, ...]


def deduplicate_tasks(tasks: tuple[ResearchTaskDraft, ...]) -> tuple[ResearchTaskDraft, ...]:
    """Deduplicate tasks by provider and normalized query while preserving order."""
    seen: set[tuple[str, str]] = set()
    unique: list[ResearchTaskDraft] = []
    for task in tasks:
        key: tuple[str, str] = (task.provider.casefold(), " ".join(task.query.split()).casefold())
        if key not in seen:
            unique.append(task)
            seen.add(key)
    return tuple(unique)


def reindex_research_aliases(
    contexts: tuple[ResearchCumulativeContext, ...],
) -> tuple[tuple[ResearchEvidenceRecord, ...], tuple[EvidenceAliasBinding, ...]]:
    """Assign globally unique aliases while preserving each exact durable binding."""
    counters: dict[str, int] = {"E": 0, "F": 0, "T": 0}
    evidence: list[ResearchEvidenceRecord] = []
    bindings: list[EvidenceAliasBinding] = []
    for context in contexts:
        evidence_by_alias = {item.alias: item for item in context.evidence}
        bindings_by_alias = {item.alias: item for item in context.alias_bindings}
        if len(evidence_by_alias) != len(context.evidence) or len(bindings_by_alias) != len(
            context.alias_bindings,
        ):
            raise ResearchAliasProjectionError("One research context repeats an evidence alias")
        if evidence_by_alias.keys() != bindings_by_alias.keys():
            raise ResearchAliasProjectionError(
                "Research evidence and durable alias bindings must have identical aliases",
            )
        for item in context.evidence:
            prefix = item.alias[0]
            counters[prefix] += 1
            alias = f"{prefix}{counters[prefix]:06d}"
            evidence.append(item.model_copy(update={"alias": alias}))
            bindings.append(bindings_by_alias[item.alias].model_copy(update={"alias": alias}))
    return tuple(evidence), tuple(bindings)


def research_candidate(  # noqa: C901 - explicit hard-stop branches belong to this bounded loop
    candidate: CandidateThesis,
    *,
    run_id: str,
    requested_as_of: datetime,
    historical_explicit: bool,
    initial_tasks: tuple[ResearchTaskDraft, ...],
    runner: ResearchRoundRunner,
    planner: ResearchPlanningAgent,
    task_memory: ResearchTaskMemory,
    budget: ResearchBudget,
    clock: Callable[[], datetime],
    prompt_character_budget: int,
    allowed_provider_names: tuple[str, ...],
) -> CandidateResearchSummary:
    """Run research until a typed stop condition or deterministic bound is reached."""
    started_at: datetime = clock()
    deadline: datetime = started_at + budget.maximum_elapsed
    session_id: str = runner.start_session(
        run_id=run_id,
        candidate=candidate,
        started_at=started_at,
        deadline=deadline,
        maximum_rounds=budget.maximum_rounds,
        maximum_queries=budget.maximum_queries,
        maximum_fetches=budget.maximum_fetches,
    )
    executions: list[ResearchRoundExecution] = []
    queries_used: int = 0
    fetches_used: int = 0
    provenance_seen: set[str] = set()
    query_keys_seen: set[tuple[str, str]] = set()
    pending: tuple[ResearchTaskDraft, ...] = deduplicate_tasks(initial_tasks)
    stop_reason: ResearchStopReason | None = None
    planner_requests: list[ResearchPlanningRequest] = []
    planner_responses: list[ResearchRoundPlan] = []

    def cumulative_context() -> ResearchCumulativeContext:
        round_contexts = tuple(execution.context for execution in executions if execution.context is not None)
        assessments = tuple(context.material_anchor_assessment for context in round_contexts)
        material_keys = tuple(dict.fromkeys((key for task in initial_tasks for key in task.material_claim_keys)))
        material_keys = tuple(
            dict.fromkeys((*material_keys, *(key for item in assessments for key in item.material_claim_keys)))
        )
        contradicted = tuple(dict.fromkeys(key for item in assessments for key in item.contradicted_claim_keys))
        supported_raw = tuple(dict.fromkeys(key for item in assessments for key in item.supported_claim_keys))
        supported = tuple(key for key in supported_raw if key not in contradicted)
        provisional_groups: dict[str, set[str]] = {}
        provisional_primary: dict[str, bool] = {}
        for assessment in assessments:
            for item in assessment.provisional_evidence:
                provisional_groups.setdefault(item.claim_key, set()).update(item.provenance_groups)
                provisional_primary[item.claim_key] = (
                    provisional_primary.get(item.claim_key, False) or item.has_authoritative_primary
                )
        provisional_evidence = tuple(
            ProvisionalAnchorEvidence(
                claim_key=key,
                provenance_groups=tuple(sorted(groups)),
                has_authoritative_primary=provisional_primary.get(key, False),
            )
            for key, groups in sorted(provisional_groups.items())
        )
        provisional_raw = tuple(
            item.claim_key
            for item in provisional_evidence
            if item.has_authoritative_primary or len(item.provenance_groups) >= 2
        )
        provisional = tuple(key for key in provisional_raw if key not in supported and key not in contradicted)
        unresolved = tuple(
            key for key in material_keys if key not in supported and key not in provisional and key not in contradicted
        )
        observations = tuple(
            {item.observation_id: item for context in round_contexts for item in context.new_observations}.values()
        )
        evidence, bindings = reindex_research_aliases(round_contexts)
        return ResearchCumulativeContext(
            new_observations=observations,
            evidence=evidence,
            alias_bindings=bindings,
            provenance_groups=tuple(sorted(provenance_seen)),
            failure_kinds=tuple(kind for execution in executions for kind in execution.failure_kinds),
            material_anchor_assessment=MaterialAnchorAssessment(
                material_claim_keys=material_keys,
                supported_claim_keys=supported,
                provisionally_covered_claim_keys=provisional,
                provisional_evidence=provisional_evidence,
                contradicted_claim_keys=contradicted,
                unresolved_claim_keys=unresolved,
                independent_provenance_groups=tuple(sorted(provenance_seen)),
                has_authoritative_primary=any(item.has_authoritative_primary for item in assessments),
                evidence_standard_satisfied=bool(material_keys) and not unresolved and not contradicted,
                decisive_contradiction=any(item.decisive_contradiction for item in assessments),
            ),
        )

    for round_number in range(1, budget.maximum_rounds + 1):
        remaining_queries: int = budget.maximum_queries - queries_used
        remaining_fetches: int = budget.maximum_fetches - fetches_used
        if clock() >= deadline or remaining_queries <= 0 or remaining_fetches <= 0:
            stop_reason = ResearchStopReason.BUDGET_EXPIRED
            break
        if not pending:
            planning_request = ResearchPlanningRequest(
                candidate=candidate,
                completed_rounds=tuple(executions),
                remaining_queries=remaining_queries,
                remaining_fetches=remaining_fetches,
                deadline=deadline,
                requested_as_of=requested_as_of,
                context_known_at=clock(),
                cumulative_context=cumulative_context(),
                allowed_provider_names=allowed_provider_names,
            )
            planning_request = _bound_planning_request(planning_request, prompt_character_budget)
            plan = planner(planning_request)
            unknown_providers = {
                task.provider for task in plan.tasks if task.provider not in frozenset(allowed_provider_names)
            }
            if unknown_providers:
                raise UnknownResearchProviderError(
                    f"Research planner requested unconfigured providers: {sorted(unknown_providers)}"
                )
            planner_requests.append(planning_request)
            planner_responses.append(plan)
            if plan.stop_reason is not None:
                _validate_stop_reason(plan.stop_reason, planning_request)
                stop_reason = plan.stop_reason
                break
            pending = deduplicate_tasks(
                tuple(
                    task.model_copy(update={"candidate_thesis_id": candidate.candidate_thesis_id})
                    for task in plan.tasks
                ),
            )
            if not pending:
                stop_reason = ResearchStopReason.UNRESOLVED
                break
            for task in pending:
                _ = task_memory.append_task(task, run_id=run_id, known_at=clock())

        novel_pending = tuple(
            task
            for task in pending
            if (task.provider.casefold(), " ".join(task.query.split()).casefold()) not in query_keys_seen
        )
        if not novel_pending:
            stop_reason = ResearchStopReason.NO_NEW_INDEPENDENT_PROVENANCE
            break
        selected: tuple[ResearchTaskDraft, ...] = novel_pending[:remaining_queries]
        query_keys_seen.update((task.provider.casefold(), " ".join(task.query.split()).casefold()) for task in selected)
        execution: ResearchRoundExecution = runner.run_round(
            session_id=session_id,
            run_id=run_id,
            candidate=candidate,
            round_number=round_number,
            tasks=selected,
            requested_as_of=requested_as_of,
            decision_at=clock(),
            historical_explicit=historical_explicit,
            query_budget=remaining_queries,
            fetch_budget=remaining_fetches,
        )
        if execution.query_count > remaining_queries or execution.fetch_count > remaining_fetches:
            raise ResearchBudgetViolationError("Research service exceeded its assigned query or fetch budget")
        executions.append(execution)
        queries_used += execution.query_count
        fetches_used += execution.fetch_count
        new_provenance: set[str] = set(execution.independent_provenance_groups) - provenance_seen
        provenance_seen.update(execution.independent_provenance_groups)
        assessment = cumulative_context().material_anchor_assessment
        if assessment.decisive_contradiction:
            stop_reason = ResearchStopReason.DECISIVE_CONTRADICTION
            break
        if assessment.evidence_standard_satisfied:
            stop_reason = ResearchStopReason.EVIDENCE_STANDARD_SATISFIED
            break
        if not new_provenance and executions:
            stop_reason = ResearchStopReason.NO_NEW_INDEPENDENT_PROVENANCE
            break
        pending = ()

    if stop_reason is None:
        stop_reason = ResearchStopReason.BUDGET_EXPIRED if clock() >= deadline else ResearchStopReason.UNRESOLVED
    summary = CandidateResearchSummary(
        candidate_thesis_id=candidate.candidate_thesis_id,
        session_id=session_id,
        rounds=tuple(executions),
        stop_reason=stop_reason,
        planner_requests=tuple(planner_requests),
        planner_responses=tuple(planner_responses),
    )
    finalized_at = clock()
    _ = runner.finalize_session(
        session_id,
        run_id=run_id,
        reason=stop_reason,
        stopped_at=finalized_at,
        known_at=finalized_at,
        summary_payload=_candidate_summary_payload(summary),
    )
    return summary


def _candidate_summary_payload(summary: CandidateResearchSummary) -> JsonValue:
    contexts = tuple(execution.context for execution in summary.rounds if execution.context is not None)
    return {
        "candidate": summary.model_dump(mode="json"),
        "contexts": [_research_context_payload(context) for context in contexts],
        "alias_bindings": [
            binding.model_dump(mode="json") for context in contexts for binding in context.alias_bindings
        ],
    }


def _research_context_payload(context: ResearchCumulativeContext) -> dict[str, JsonValue]:
    return context.model_dump(mode="json")


def _validate_stop_reason(reason: ResearchStopReason, request: ResearchPlanningRequest) -> None:
    """Reject agent-authored stop claims that deterministic state cannot prove."""
    assessment = request.cumulative_context.material_anchor_assessment
    if reason is ResearchStopReason.EVIDENCE_STANDARD_SATISFIED and not assessment.evidence_standard_satisfied:
        raise InvalidResearchStopReasonError("Evidence-standard stop is not supported by deterministic assessment")
    if reason is ResearchStopReason.DECISIVE_CONTRADICTION and not assessment.decisive_contradiction:
        raise InvalidResearchStopReasonError("Contradiction stop is not supported by deterministic assessment")
    if (
        reason is ResearchStopReason.BUDGET_EXPIRED
        and request.remaining_queries > 0
        and request.remaining_fetches > 0
        and request.context_known_at < request.deadline
    ):
        raise InvalidResearchStopReasonError("Budget-expired stop was proposed before a hard budget was exhausted")


def make_research_node(
    *,
    theses: ThesisMemory,
    tasks: ResearchTaskMemory,
    runner: ResearchRoundRunner,
    planner: ResearchPlanningAgent,
    implementation_version: str,
    admission: IntelligenceAdmissionRepository,
    budget: ResearchBudget | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
    model_point_in_time_certified: bool = False,
    prompt_character_budget: int = 120_000,
    allowed_provider_names: tuple[str, ...],
) -> PipelineNode:
    """Return A3 with read-only provider execution and durable research bookkeeping."""
    resolved_budget: ResearchBudget = budget or ResearchBudget()
    resolved_prompt_character_budget = min(
        prompt_character_budget,
        request_character_allowance(planner, fallback=prompt_character_budget),
    )

    def node(state: PipelineState) -> PipelineState:
        require_predecessor(state, Stage.A3)
        run_id: str = require_run_id(state)
        requested_as_of: datetime = require_requested_as_of(state)
        decision_at: datetime = clock()
        started_at: datetime = require_run_started_at(state)
        recovered = runner.recover_stage_admission(run_id=run_id, known_at=clock())
        if recovered is not None:
            return _admit_recovered_research_stage(
                state,
                recovered=recovered,
                admission=admission,
                implementation_version=implementation_version,
                decision_at=decision_at,
            )
        require_model_temporal_authority(
            requested_as_of=requested_as_of,
            run_started_at=started_at,
            requested_as_of_explicit=state.get("requested_as_of_explicit", False),
            point_in_time_certified=model_point_in_time_certified,
        )
        selected_ids: set[str] = set(state.get("candidate_thesis_ids", ()))
        candidates: tuple[CandidateThesis, ...] = theses.candidates_due_for_research(
            as_of=requested_as_of,
            exact_candidate_ids=tuple(sorted(selected_ids)),
        )
        _ = tuple(
            research_candidate(
                candidate,
                run_id=run_id,
                requested_as_of=requested_as_of,
                historical_explicit=(state.get("requested_as_of_explicit", False) and requested_as_of < started_at),
                initial_tasks=tasks.pending_for_candidate(
                    candidate.candidate_thesis_id,
                    run_id=run_id,
                    as_of=decision_at,
                ),
                runner=runner,
                planner=planner,
                task_memory=tasks,
                budget=resolved_budget,
                clock=clock,
                prompt_character_budget=resolved_prompt_character_budget,
                allowed_provider_names=allowed_provider_names,
            )
            for candidate in candidates
        )
        decision_at = clock()
        artifact_known_at = clock()
        recovered = runner.recover_stage_admission(
            run_id=run_id,
            known_at=artifact_known_at,
        )
        if recovered is None:
            empty_payload = ResearchArtifactPayload(candidates=(), contexts=(), alias_bindings=())
            stage_admission = runner.stage_admission(
                run_id=run_id,
                session_ids=(),
                known_at=artifact_known_at,
            )
            recovered = RecoveredResearchStage(
                admission=stage_admission,
                payload=empty_payload.model_dump(mode="json"),
            )
        return _admit_recovered_research_stage(
            state,
            recovered=recovered,
            admission=admission,
            implementation_version=implementation_version,
            decision_at=decision_at,
        )

    return node


def _admit_recovered_research_stage(
    state: PipelineState,
    *,
    recovered: RecoveredResearchStage,
    admission: IntelligenceAdmissionRepository,
    implementation_version: str,
    decision_at: datetime,
) -> PipelineState:
    payload = ResearchArtifactPayload.model_validate(recovered.payload)
    binding_aliases = tuple(binding.alias for binding in payload.alias_bindings)
    context_aliases = tuple(item.alias for context in payload.contexts for item in context.evidence)
    if len(binding_aliases) != len(set(binding_aliases)) or len(context_aliases) != len(set(context_aliases)):
        raise ResearchAliasProjectionError(
            "Recovered research aliases must be globally unique across the admitted A3 payload"
        )
    bindings_by_alias = {binding.alias: binding for binding in payload.alias_bindings}
    if bindings_by_alias.keys() != set(context_aliases):
        raise ResearchAliasProjectionError(
            "Recovered research evidence and durable alias bindings must have identical aliases"
        )
    recovered_contexts = tuple(
        context.model_copy(
            update={
                "alias_bindings": tuple(bindings_by_alias[item.alias] for item in context.evidence),
            }
        )
        for context in payload.contexts
    )
    stage_admission = recovered.admission
    artifact = build_stage_artifact(
        run_id=require_run_id(state),
        stage=Stage.A3,
        requested_as_of=require_requested_as_of(state),
        started_at=require_run_started_at(state),
        known_at=stage_admission.known_at,
        decision_at=decision_at,
        input_ids=stage_admission.input_ids(),
        output_ids=stage_admission.output_ids(),
        implementation_version=implementation_version,
        payload=payload,
    )
    admission.admit_research_stage(stage_admission, artifact=artifact)
    _ = try_install_stage_artifact_file(require_run_dir(state), artifact)
    return {
        "completed_stages": completed_with(state, Stage.A3.value),
        "artifact_ids": (*state.get("artifact_ids", ()), artifact.artifact_id),
        "observation_ids": (*state.get("observation_ids", ()), *stage_admission.observation_ids),
        "evidence_fragment_ids": (*state.get("evidence_fragment_ids", ()), *stage_admission.fragment_ids),
        "interpretation_attempt_ids": (
            *state.get("interpretation_attempt_ids", ()),
            *stage_admission.interpretation_attempt_ids,
        ),
        "research_failure_ids": (*state.get("research_failure_ids", ()), *stage_admission.failure_ids),
        "research_contexts": (*state.get("research_contexts", ()), *recovered_contexts),
        "decision_at": decision_at,
    }


def _bound_planning_request(
    request: ResearchPlanningRequest,
    character_budget: int,
) -> ResearchPlanningRequest:
    """Greedily project cumulative records against the exact serialized request size."""
    context = request.cumulative_context
    all_ids = tuple(item.observation_id for item in context.new_observations) + tuple(
        item.alias for item in context.evidence
    )
    selected_observations: list[ClaimObservation] = []
    selected_evidence: list[ResearchEvidenceRecord] = []
    selected_ids: list[str] = []

    def candidate_request(
        observations: tuple[ClaimObservation, ...],
        evidence: tuple[ResearchEvidenceRecord, ...],
        ids: tuple[str, ...],
    ) -> ResearchPlanningRequest:
        aliases = {item.alias for item in evidence}
        candidate_context = context.model_copy(
            update={
                "new_observations": observations,
                "evidence": evidence,
                "alias_bindings": tuple(binding for binding in context.alias_bindings if binding.alias in aliases),
            },
        )
        return request.model_copy(
            update={
                "cumulative_context": candidate_context,
                "omitted_input_ids": tuple(item for item in all_ids if item not in ids),
            },
        )

    for record in context.new_observations:
        identifier = record.observation_id
        atomic = candidate_request((record,), (), (identifier,))
        if serialized_inference_request_size(atomic) > character_budget:
            raise PromptProjectionError(f"Atomic research-planning record exceeds budget: {identifier}")
        candidate = candidate_request(
            (*selected_observations, record), tuple(selected_evidence), (*selected_ids, identifier)
        )
        if serialized_inference_request_size(candidate) <= character_budget:
            selected_observations.append(record)
            selected_ids.append(identifier)
    for record in context.evidence:
        identifier = record.alias
        atomic = candidate_request((), (record,), (identifier,))
        if serialized_inference_request_size(atomic) > character_budget:
            raise PromptProjectionError(f"Atomic research-planning record exceeds budget: {identifier}")
        candidate = candidate_request(
            tuple(selected_observations), (*selected_evidence, record), (*selected_ids, identifier)
        )
        if serialized_inference_request_size(candidate) <= character_budget:
            selected_evidence.append(record)
            selected_ids.append(identifier)
    bounded = candidate_request(tuple(selected_observations), tuple(selected_evidence), tuple(selected_ids))
    if serialized_inference_request_size(bounded) > character_budget:
        raise PromptProjectionError("Research-planning fixed context exceeds the character budget")
    return bounded
