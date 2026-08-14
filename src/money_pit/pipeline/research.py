"""Module implementing the bounded iterative A3 research loop."""

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import TypeAdapter

from money_pit.agents.budget import request_character_allowance
from money_pit.agents.budget import serialized_inference_request_size
from money_pit.agents.inference import InferenceInvocationContext
from money_pit.contracts import ClaimMemory
from money_pit.contracts import ResearchPlanningAgent
from money_pit.contracts import ResearchPlanningRequest
from money_pit.contracts import ResearchProgressDigest
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
from money_pit.pipeline.identity import stable_identifier
from money_pit.pipeline.temporal import require_model_temporal_authority
from money_pit.schemas.research import EvidenceAliasBinding
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import ProvisionalAnchorEvidence
from money_pit.schemas.research import RecoveredResearchStage
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.research import ResearchEvidenceRecord
from money_pit.schemas.research import ResearchStopReason
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import bind_artifact_record
from money_pit.schemas.theses import CandidateThesis
from money_pit.storage.admission import IntelligenceAdmissionRepository
from money_pit.storage.intelligence_work import ClaimedResearchJobRecord
from money_pit.storage.intelligence_work import IncrementalResearchAdmissionRecord
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import ResearchCheckpointRecord
from money_pit.storage.intelligence_work import ResearchJobRecord
from money_pit.storage.intelligence_work import SynthesisUnitRecord
from money_pit.storage.intelligence_work import WorkClaim


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
    maximum_queries: int = Field(default=6, ge=1)
    maximum_fetches: int = Field(default=12, ge=1)
    maximum_elapsed: timedelta = Field(default=timedelta(minutes=10), gt=timedelta(0))


_MAXIMUM_CANDIDATES_PER_UPDATE = 2
_MAXIMUM_QUERIES_PER_WAVE = 2
_MAXIMUM_FETCHES_PER_WAVE = 4
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class CandidateResearchSummary(BaseModel):
    """Immutable A3 summary for one candidate."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    candidate_thesis_id: str
    session_id: str
    rounds: tuple[ResearchRoundExecution, ...]
    stop_reason: ResearchStopReason
    planner_requests: tuple[ResearchPlanningRequest, ...] = ()
    planner_responses: tuple[ResearchRoundPlan, ...] = ()
    normalized_queries: tuple[str, ...] = ()


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


def _select_research_tasks(
    tasks: tuple[ResearchTaskDraft, ...],
    *,
    query_budget: int,
    fetch_budget: int,
) -> tuple[ResearchTaskDraft, ...]:
    """Select only tasks that can receive one query and one fetch."""
    admitted_count: int = min(len(tasks), query_budget, fetch_budget)
    if admitted_count <= 0:
        return ()
    return tasks[:admitted_count]


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
    job_id: str | None = None,
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
    prior_checkpoint: ResearchCheckpointRecord | None = None,
    checkpoint_wave: Callable[[ResearchCheckpointRecord], None] | None = None,
) -> CandidateResearchSummary:
    """Run research until a typed stop condition or deterministic bound is reached."""
    started_at: datetime = clock()
    deadline: datetime = started_at + budget.maximum_elapsed
    session_id: str = runner.resume_or_start_session(
        run_id=run_id,
        candidate=candidate,
        started_at=started_at,
        deadline=deadline,
        maximum_rounds=budget.maximum_rounds,
        maximum_queries=budget.maximum_queries,
        maximum_fetches=budget.maximum_fetches,
    )
    prior_summary, prior_contexts = _restore_research_checkpoint(prior_checkpoint)
    executions: list[ResearchRoundExecution] = []
    queries_used: int = 0 if prior_checkpoint is None else prior_checkpoint.search_count
    fetches_used: int = 0 if prior_checkpoint is None else prior_checkpoint.accepted_fetch_count
    provenance_seen: set[str] = {group for context in prior_contexts for group in context.provenance_groups}
    query_keys_seen: set[tuple[str, str]] = set()
    for value in () if prior_summary is None else prior_summary.normalized_queries:
        provider, separator, query = value.partition(":")
        if separator:
            query_keys_seen.add((provider, query))
    resumed_tasks = runner.pending_tasks_for_session(session_id)
    durable_planner_tasks = task_memory.pending_for_candidate(
        candidate.candidate_thesis_id,
        run_id=run_id,
        as_of=started_at,
    )
    pending: tuple[ResearchTaskDraft, ...] = deduplicate_tasks(resumed_tasks or durable_planner_tasks or initial_tasks)
    stop_reason: ResearchStopReason | None = None
    planner_requests: list[ResearchPlanningRequest] = []
    planner_responses: list[ResearchRoundPlan] = []

    def cumulative_context() -> ResearchCumulativeContext:
        round_contexts = (
            *prior_contexts,
            *(execution.context for execution in executions if execution.context is not None),
        )
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

    starting_wave = 1 if prior_checkpoint is None else prior_checkpoint.wave_number + 1
    recovered_assessment = cumulative_context().material_anchor_assessment
    if recovered_assessment.decisive_contradiction:
        stop_reason = ResearchStopReason.DECISIVE_CONTRADICTION
    elif recovered_assessment.evidence_standard_satisfied:
        stop_reason = ResearchStopReason.EVIDENCE_STANDARD_SATISFIED
    for round_number in range(starting_wave, min(budget.maximum_rounds, starting_wave) + 1):
        if stop_reason is not None:
            break
        remaining_queries: int = budget.maximum_queries - queries_used
        remaining_fetches: int = budget.maximum_fetches - fetches_used
        if clock() >= deadline or remaining_queries <= 0 or remaining_fetches <= 0:
            stop_reason = ResearchStopReason.BUDGET_EXPIRED
            break
        if not pending:
            planning_request = ResearchPlanningRequest(
                candidate=candidate,
                progress=ResearchProgressDigest(
                    material_anchor_assessment=cumulative_context().material_anchor_assessment,
                    provenance_groups=tuple(sorted(provenance_seen)),
                    normalized_prior_queries=tuple(
                        sorted(f"{provider}:{query}" for provider, query in query_keys_seen)
                    ),
                    accepted_fetch_count=fetches_used,
                    rejected_result_count=sum(len(item.failure_kinds) for item in executions),
                    completed_wave_count=starting_wave - 1 + len(executions),
                ),
                remaining_queries=remaining_queries,
                remaining_fetches=remaining_fetches,
                deadline=deadline,
                requested_as_of=requested_as_of,
                context_known_at=clock(),
                allowed_provider_names=allowed_provider_names,
            )
            planning_request = _bound_planning_request(planning_request, prompt_character_budget)
            durable_plan = (
                None
                if job_id is None
                else task_memory.planner_result(
                    job_id=job_id,
                    wave_number=round_number,
                )
            )
            if durable_plan is None:
                plan = planner(
                    planning_request,
                    context=InferenceInvocationContext(
                        run_id=run_id,
                        work_unit_id=job_id or f"legacy:{candidate.candidate_thesis_id}",
                    ),
                ).output
                if job_id is not None:
                    task_memory.checkpoint_planner_result(
                        job_id=job_id,
                        wave_number=round_number,
                        run_id=run_id,
                        known_at=clock(),
                        request=planning_request,
                        result=plan,
                    )
            else:
                plan = durable_plan
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
                stop_reason = (
                    ResearchStopReason.NO_NEW_INDEPENDENT_PROVENANCE
                    if plan.stop_reason is ResearchStopReason.UNRESOLVED and not plan.tasks
                    else plan.stop_reason
                )
                break
            pending = deduplicate_tasks(
                tuple(
                    task.model_copy(update={"candidate_thesis_id": candidate.candidate_thesis_id})
                    for task in plan.tasks
                ),
            )
            if not pending:
                stop_reason = ResearchStopReason.NO_NEW_INDEPENDENT_PROVENANCE
                break
            _ = task_memory.checkpoint_planner_tasks(
                pending,
                run_id=run_id,
                known_at=clock(),
            )

        novel_pending = tuple(
            task
            for task in pending
            if (task.provider.casefold(), " ".join(task.query.split()).casefold()) not in query_keys_seen
        )
        if not novel_pending:
            stop_reason = ResearchStopReason.NO_NEW_INDEPENDENT_PROVENANCE
            break
        selected: tuple[ResearchTaskDraft, ...] = _select_research_tasks(
            novel_pending,
            query_budget=min(remaining_queries, _MAXIMUM_QUERIES_PER_WAVE),
            fetch_budget=min(remaining_fetches, _MAXIMUM_FETCHES_PER_WAVE),
        )
        query_keys_seen.update((task.provider.casefold(), " ".join(task.query.split()).casefold()) for task in selected)

        checkpoint_wave_number = round_number
        checkpoint_queries_used = queries_used
        checkpoint_fetches_used = fetches_used

        def persist_completed_wave(
            completed: ResearchRoundExecution,
            wave_number: int = checkpoint_wave_number,
            prior_search_count: int = checkpoint_queries_used,
            prior_fetch_count: int = checkpoint_fetches_used,
        ) -> None:
            if checkpoint_wave is None or job_id is None:
                return
            checkpoint_summary = CandidateResearchSummary(
                candidate_thesis_id=candidate.candidate_thesis_id,
                session_id=session_id,
                rounds=(completed,),
                stop_reason=ResearchStopReason.UNRESOLVED,
                planner_requests=tuple(planner_requests),
                planner_responses=tuple(planner_responses),
                normalized_queries=tuple(sorted(f"{provider}:{query}" for provider, query in query_keys_seen)),
            )
            checkpoint_wave(
                ResearchCheckpointRecord(
                    checkpoint_id=stable_identifier(
                        "research-checkpoint",
                        {"job_id": job_id, "wave_number": wave_number},
                    ),
                    job_id=job_id,
                    run_id=run_id,
                    wave_number=wave_number,
                    search_count=prior_search_count + completed.query_count,
                    accepted_fetch_count=prior_fetch_count + completed.fetch_count,
                    recorded_at=clock(),
                    digest=_merge_checkpoint_digest(prior_checkpoint, checkpoint_summary),
                )
            )

        execution: ResearchRoundExecution = runner.run_round(
            session_id=session_id,
            job_id=job_id or f"legacy:{candidate.candidate_thesis_id}",
            run_id=run_id,
            candidate=candidate,
            round_number=round_number,
            tasks=selected,
            requested_as_of=requested_as_of,
            decision_at=clock(),
            historical_explicit=historical_explicit,
            query_budget=min(remaining_queries, _MAXIMUM_QUERIES_PER_WAVE),
            fetch_budget=min(remaining_fetches, _MAXIMUM_FETCHES_PER_WAVE),
            on_completed=persist_completed_wave,
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
        normalized_queries=tuple(sorted(f"{provider}:{query}" for provider, query in query_keys_seen)),
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


def _merge_checkpoint_digest(
    prior: ResearchCheckpointRecord | None,
    summary: CandidateResearchSummary,
) -> JsonValue:
    """Carry all durable research context into the next cumulative checkpoint."""
    current = _candidate_summary_payload(summary)
    if prior is None:
        return current
    if not isinstance(prior.digest, dict) or not isinstance(current, dict):
        raise PromptProjectionError("Research checkpoint digest must be an object")
    prior_contexts = prior.digest.get("contexts", [])
    current_contexts = current.get("contexts", [])
    if not isinstance(prior_contexts, list) or not isinstance(current_contexts, list):
        raise PromptProjectionError("Research checkpoint contexts must be a list")
    return {**current, "contexts": [*prior_contexts, *current_contexts]}


def _install_wave_checkpoint(
    work: IntelligenceWorkRepository,
    checkpoint: ResearchCheckpointRecord,
) -> None:
    """Atomically bind a validated checkpoint to its durable wave outbox."""
    wave = work.uncheckpointed_wave(checkpoint.job_id)
    if wave is None:
        raise PromptProjectionError("Research checkpoint has no execution-completed wave predecessor")
    if checkpoint.run_id is None:
        raise PromptProjectionError("Research checkpoint requires an owning run")
    _ = work.checkpoint_completed_wave(
        wave_result_id=wave.wave_result_id,
        checkpoint_run_id=checkpoint.run_id,
        digest=checkpoint.digest,
        recorded_at=checkpoint.recorded_at,
    )


def _recover_wave_checkpoint(
    work: IntelligenceWorkRepository,
    job: ClaimedResearchJobRecord,
) -> None:
    """Reconcile a completed wave after a hard kill before its callback."""
    wave = work.uncheckpointed_wave(job.job_id)
    if wave is None:
        return
    if not isinstance(wave.execution, dict):
        raise PromptProjectionError("Research wave outbox must be an object")
    execution_value = wave.execution
    context_value = wave.context
    execution = ResearchRoundExecution.model_validate(
        {
            **execution_value,
            "context": (None if context_value is None else ResearchCumulativeContext.model_validate(context_value)),
        }
    )
    prior = work.latest_research_checkpoint(job.job_id)
    summary = CandidateResearchSummary(
        candidate_thesis_id=job.candidate_thesis_id,
        session_id=wave.session_id,
        rounds=(execution,),
        stop_reason=ResearchStopReason.UNRESOLVED,
        normalized_queries=(),
    )
    if job.claimed_run_id is None or job.claimed_at is None:
        raise PromptProjectionError("Research recovery requires a current work claim")
    _ = work.checkpoint_completed_wave(
        wave_result_id=wave.wave_result_id,
        checkpoint_run_id=job.claimed_run_id,
        digest=_merge_checkpoint_digest(prior, summary),
        recorded_at=job.claimed_at,
    )


def _checkpoint_writer(
    work: IntelligenceWorkRepository,
) -> Callable[[ResearchCheckpointRecord], None]:
    return lambda checkpoint: _install_wave_checkpoint(work, checkpoint)


def _recover_claimed_waves(
    work: IntelligenceWorkRepository,
    jobs: tuple[ClaimedResearchJobRecord, ...],
) -> None:
    for job in jobs:
        _recover_wave_checkpoint(work, job)


def _next_research_review(
    digest: JsonValue,
    completed_at: datetime,
    *,
    material_claim_refreshes: tuple[datetime, ...] = (),
) -> datetime | None:
    """Return the earliest future claim review or evidence expiry."""
    context_values = digest.get("contexts") if isinstance(digest, dict) else None
    if not isinstance(context_values, list):
        expiries: tuple[datetime, ...] = ()
    else:
        expiries = tuple(
            observation.valid_until
            for value in context_values
            for observation in ResearchCumulativeContext.model_validate(value).new_observations
            if observation.valid_until is not None and observation.valid_until > completed_at
        )
    due = (*expiries, *(value for value in material_claim_refreshes if value > completed_at))
    return min(due) if due else None


def _material_claim_keys(job: ResearchJobRecord) -> tuple[str, ...]:
    """Recover exact material claim identities from the immutable job premise."""
    if not isinstance(job.payload, dict):
        return ()
    premises = job.payload.get("premises")
    if not isinstance(premises, dict):
        return ()
    values = premises.get("material_claims")
    if not isinstance(values, list):
        return ()
    return tuple(
        key
        for value in values
        if isinstance(value, dict) and isinstance((key := value.get("canonical_claim_key")), str)
    )


def _restore_research_checkpoint(
    checkpoint: ResearchCheckpointRecord | None,
) -> tuple[CandidateResearchSummary | None, tuple[ResearchCumulativeContext, ...]]:
    """Restore compact progress and durable context from the latest completed wave."""
    if checkpoint is None:
        return None, ()
    if not isinstance(checkpoint.digest, dict):
        raise PromptProjectionError("Research checkpoint digest must be an object")
    summary_value = checkpoint.digest.get("candidate")
    contexts_value = checkpoint.digest.get("contexts")
    if "backfilled_session_id" in checkpoint.digest:
        return None, ()
    if not isinstance(summary_value, dict) or not isinstance(contexts_value, list):
        raise PromptProjectionError("Research checkpoint digest is incomplete")
    return (
        CandidateResearchSummary.model_validate(summary_value),
        tuple(ResearchCumulativeContext.model_validate(value) for value in contexts_value),
    )


def _research_context_payload(context: ResearchCumulativeContext) -> dict[str, JsonValue]:
    payload = context.model_dump(mode="json")
    payload["alias_bindings"] = [binding.model_dump(mode="json") for binding in context.alias_bindings]
    return payload


def _validate_stop_reason(reason: ResearchStopReason, request: ResearchPlanningRequest) -> None:
    """Reject agent-authored stop claims that deterministic state cannot prove."""
    assessment = request.progress.material_anchor_assessment
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


def make_research_node(  # noqa: C901 - explicit incremental recovery branches remain visible
    *,
    claims: ClaimMemory,
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
    work_repository: IntelligenceWorkRepository | None = None,
) -> PipelineNode:
    """Return A3 with read-only provider execution and durable research bookkeeping."""
    resolved_budget: ResearchBudget = budget or ResearchBudget()
    resolved_prompt_character_budget = min(
        prompt_character_budget,
        request_character_allowance(planner, fallback=prompt_character_budget),
    )

    def node(state: PipelineState) -> PipelineState:  # noqa: C901 - bounded stage orchestration
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
        claimed_jobs = ()
        if work_repository is not None:
            selected_ids.update(work_repository.candidates_for_research_reconciliation(state.get("source_id")))
            for candidate_id in sorted(selected_ids):
                candidate = theses.candidates_by_ids((candidate_id,))[0]
                origin_unit_ids = work_repository.discovery_origin_unit_ids(
                    candidate_id,
                    state.get("source_id"),
                )
                initial_tasks = tasks.pending_for_candidate(
                    candidate_id,
                    run_id=run_id,
                    as_of=requested_as_of,
                    origin_unit_ids=origin_unit_ids,
                )
                latest_job = work_repository.latest_research_job_for_candidate(
                    candidate_id,
                    state.get("source_id"),
                )
                if not initial_tasks and latest_job is not None:
                    initial_tasks = _research_job_tasks(latest_job)
                premise_claim_keys = tuple(
                    dict.fromkeys(key for task in initial_tasks for key in task.material_claim_keys)
                )
                projections = claims.projections_with_deltas(
                    requested_as_of=requested_as_of,
                    observation_ids=state.get("observation_ids", ()),
                    resolution_ids=state.get("claim_resolution_decision_ids", ()),
                    verification_ids=state.get("verification_result_ids", ()),
                )
                material_projections = tuple(
                    projection.model_dump(mode="json", exclude={"projected_as_of"})
                    for projection in projections
                    if projection.canonical_claim_key in premise_claim_keys
                )
                premise_payload = _JSON_VALUE_ADAPTER.validate_python(
                    {
                        "candidate": candidate.model_dump(mode="json"),
                        "discovery_origin_unit_ids": list(origin_unit_ids),
                        "material_claims": list(material_projections),
                        "tasks": [task.model_dump(mode="json") for task in deduplicate_tasks(initial_tasks)],
                    }
                )
                premise_fingerprint = hashlib.sha256(
                    json.dumps(premise_payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                job_id = stable_identifier(
                    "research-job",
                    {"candidate_id": candidate_id, "premise_fingerprint": premise_fingerprint},
                )
                work_repository.ensure_research_job(
                    ResearchJobRecord(
                        job_id=job_id,
                        candidate_thesis_id=candidate_id,
                        premise_fingerprint=premise_fingerprint,
                        source_discovery_unit_id=(origin_unit_ids[0] if len(origin_unit_ids) == 1 else None),
                        created_at=candidate.created_at,
                        payload={"candidate_id": candidate_id, "premises": premise_payload},
                    ),
                    origin_unit_ids=origin_unit_ids,
                )
            claimed_at = clock()
            claim = WorkClaim(run_id=run_id, claimed_at=claimed_at)
            claimed_jobs = work_repository.claim_research_jobs(
                run_id=claim.run_id,
                claimed_at=claim.claimed_at,
                reclaim_before=claimed_at - timedelta(minutes=30),
                maximum_jobs=_MAXIMUM_CANDIDATES_PER_UPDATE,
                source_id=state.get("source_id"),
            )
            _recover_claimed_waves(work_repository, claimed_jobs)
            selected_ids = {job.candidate_thesis_id for job in claimed_jobs}
            if not claimed_jobs:
                return {
                    "completed_stages": completed_with(state, Stage.A3.value),
                    "artifact_ids": state.get("artifact_ids", ()),
                    "observation_ids": state.get("observation_ids", ()),
                    "evidence_fragment_ids": state.get("evidence_fragment_ids", ()),
                    "interpretation_attempt_ids": state.get("interpretation_attempt_ids", ()),
                    "research_failure_ids": state.get("research_failure_ids", ()),
                    "research_contexts": state.get("research_contexts", ()),
                    "decision_at": clock(),
                }
        candidates: tuple[CandidateThesis, ...] = (
            theses.candidates_due_for_research(
                as_of=requested_as_of,
                exact_candidate_ids=tuple(sorted(selected_ids)),
            )
            if work_repository is None
            else theses.candidates_by_ids(tuple(sorted(selected_ids)))
        )[:_MAXIMUM_CANDIDATES_PER_UPDATE]
        summaries = tuple(
            research_candidate(
                candidate,
                run_id=run_id,
                job_id=(
                    None
                    if work_repository is None
                    else next(
                        job.job_id for job in claimed_jobs if job.candidate_thesis_id == candidate.candidate_thesis_id
                    )
                ),
                requested_as_of=requested_as_of,
                historical_explicit=(state.get("requested_as_of_explicit", False) and requested_as_of < started_at),
                initial_tasks=(
                    tasks.pending_for_candidate(
                        candidate.candidate_thesis_id,
                        run_id=run_id,
                        as_of=decision_at,
                    )
                    if work_repository is None
                    else _research_job_tasks(
                        next(job for job in claimed_jobs if job.candidate_thesis_id == candidate.candidate_thesis_id)
                    )
                ),
                runner=runner,
                planner=planner,
                task_memory=tasks,
                budget=resolved_budget,
                clock=clock,
                prompt_character_budget=resolved_prompt_character_budget,
                allowed_provider_names=allowed_provider_names,
                prior_checkpoint=(
                    None
                    if work_repository is None
                    else work_repository.latest_research_checkpoint(
                        next(
                            job.job_id
                            for job in claimed_jobs
                            if job.candidate_thesis_id == candidate.candidate_thesis_id
                        )
                    )
                ),
                checkpoint_wave=(None if work_repository is None else _checkpoint_writer(work_repository)),
            )
            for candidate in candidates
        )
        if work_repository is not None:
            jobs_by_candidate = {job.candidate_thesis_id: job for job in claimed_jobs}
            for summary in summaries:
                job = jobs_by_candidate[summary.candidate_thesis_id]
                searches = sum(item.query_count for item in summary.rounds)
                fetches = sum(item.fetch_count for item in summary.rounds)
                wave_number = min(3, job.wave_count + len(summary.rounds))
                search_count = min(6, job.search_count + searches)
                fetch_count = min(12, job.accepted_fetch_count + fetches)
                prior_checkpoint = work_repository.latest_research_checkpoint(job.job_id)
                digest = _candidate_summary_payload(summary) if prior_checkpoint is None else prior_checkpoint.digest
                terminal = (
                    summary.stop_reason
                    in {
                        ResearchStopReason.EVIDENCE_STANDARD_SATISFIED,
                        ResearchStopReason.DECISIVE_CONTRADICTION,
                        ResearchStopReason.NO_NEW_INDEPENDENT_PROVENANCE,
                    }
                    or wave_number >= 3
                    or search_count >= 6
                    or fetch_count >= 12
                )
                if terminal:
                    completed_at = clock()
                    material_claim_keys = set(_material_claim_keys(job))
                    material_claim_refreshes = tuple(
                        projection.next_refresh_at
                        for projection in claims.projections_as_of(as_of=completed_at)
                        if projection.canonical_claim_key in material_claim_keys
                        and projection.next_refresh_at is not None
                    )
                    work_repository.finalize_research_job(
                        job_id=job.job_id,
                        completed_at=completed_at,
                        stop_reason=summary.stop_reason.value,
                        next_review_at=_next_research_review(
                            digest,
                            completed_at,
                            material_claim_refreshes=material_claim_refreshes,
                        ),
                    )
                    input_fingerprint = hashlib.sha256(
                        json.dumps(digest, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    origin_units = work_repository.discovery_units_by_ids(
                        work_repository.discovery_origin_unit_ids(
                            summary.candidate_thesis_id,
                            state.get("source_id"),
                        )
                    )
                    origin_observation_ids = tuple(
                        dict.fromkeys(
                            identifier
                            for unit in origin_units
                            for identifier in _discovery_observation_ids(unit.payload)
                        )
                    )
                    work_repository.ensure_synthesis_unit(
                        SynthesisUnitRecord(
                            unit_id=stable_identifier(
                                "synthesis-unit",
                                {"job_id": job.job_id, "input_fingerprint": input_fingerprint},
                            ),
                            research_job_id=job.job_id,
                            input_fingerprint=input_fingerprint,
                            created_at=clock(),
                            payload={
                                "candidate_id": summary.candidate_thesis_id,
                                "context": digest,
                                "origin_observation_ids": list(origin_observation_ids),
                            },
                        )
                    )
        decision_at = clock()
        artifact_known_at = clock()
        if work_repository is not None:
            return _admit_incremental_research_stage(
                state,
                summaries=summaries,
                admission=admission,
                work=work_repository,
                jobs=claimed_jobs,
                implementation_version=implementation_version,
                decision_at=decision_at,
                known_at=artifact_known_at,
            )
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


def _research_job_tasks(job: ResearchJobRecord) -> tuple[ResearchTaskDraft, ...]:
    if not isinstance(job.payload, dict):
        raise PromptProjectionError("Research job payload must be an object")
    premises = job.payload.get("premises")
    if not isinstance(premises, dict):
        raise PromptProjectionError("Research job payload has no premises")
    values = premises.get("tasks")
    if not isinstance(values, list):
        raise PromptProjectionError("Research job premise tasks must be a list")
    return deduplicate_tasks(tuple(ResearchTaskDraft.model_validate(value) for value in values))


def _admit_incremental_research_stage(
    state: PipelineState,
    *,
    summaries: tuple[CandidateResearchSummary, ...],
    admission: IntelligenceAdmissionRepository,
    work: IntelligenceWorkRepository,
    jobs: tuple[ResearchJobRecord, ...],
    implementation_version: str,
    decision_at: datetime,
    known_at: datetime,
) -> PipelineState:
    contexts = tuple(
        execution.context for summary in summaries for execution in summary.rounds if execution.context is not None
    )
    _, alias_bindings = reindex_research_aliases(contexts)
    payload = ResearchArtifactPayload(
        candidates=summaries,
        contexts=contexts,
        alias_bindings=alias_bindings,
    )
    artifact = build_stage_artifact(
        run_id=require_run_id(state),
        stage=Stage.A3,
        requested_as_of=require_requested_as_of(state),
        started_at=require_run_started_at(state),
        known_at=known_at,
        decision_at=decision_at,
        input_ids=tuple(
            bind_artifact_record(ArtifactRecordKind.CANDIDATE_THESIS, summary.candidate_thesis_id)
            for summary in summaries
        ),
        output_ids=(),
        implementation_version=implementation_version,
        payload=payload,
    )
    admission.admit_incremental_research_artifact(artifact)
    checkpoints = tuple(
        checkpoint for job in jobs if (checkpoint := work.latest_research_checkpoint(job.job_id)) is not None
    )
    wave_result_ids = tuple(
        hashlib.sha256(f"{checkpoint.job_id}\0{checkpoint.wave_number}".encode("utf-8")).hexdigest()
        for checkpoint in checkpoints
    )
    work.admit_incremental_research(
        IncrementalResearchAdmissionRecord(
            admission_id=stable_identifier(
                "incremental-research-admission",
                {"artifact_id": artifact.artifact_id},
            ),
            run_id=require_run_id(state),
            artifact_id=artifact.artifact_id,
            known_at=known_at,
            input_job_ids=tuple(job.job_id for job in jobs),
            input_wave_result_ids=wave_result_ids,
            input_checkpoint_ids=tuple(item.checkpoint_id for item in checkpoints),
            output_record_ids=artifact.output_ids,
            payload=payload.model_dump(mode="json"),
        )
    )
    _ = try_install_stage_artifact_file(require_run_dir(state), artifact)
    return {
        "completed_stages": completed_with(state, Stage.A3.value),
        "artifact_ids": (*state.get("artifact_ids", ()), artifact.artifact_id),
        "research_contexts": (*state.get("research_contexts", ()), *contexts),
        "decision_at": decision_at,
    }


def _discovery_observation_ids(payload: JsonValue) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    values = payload.get("observation_ids")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return ()
    return tuple(value for value in values if isinstance(value, str))


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
    """Project a compact decision digest without replaying fetched evidence."""
    if serialized_inference_request_size(request) > character_budget:
        raise PromptProjectionError("Research-planning fixed context exceeds the character budget")
    return request
