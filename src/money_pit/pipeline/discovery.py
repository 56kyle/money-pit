"""Module implementing A2 layered thesis discovery and research planning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable  # noqa: TC003 - used by the runtime default clock
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import TYPE_CHECKING
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import JsonValue
from pydantic import TypeAdapter

from money_pit.agents.budget import request_character_allowance
from money_pit.agents.budget import serialized_inference_request_size
from money_pit.agents.inference import InferenceInvocationContext
from money_pit.contracts import CandidateThesisDraft
from money_pit.contracts import ClaimMemory
from money_pit.contracts import DiscoveryAgent
from money_pit.contracts import DiscoveryDraft
from money_pit.contracts import DiscoveryRequest
from money_pit.contracts import DiscoverySignal
from money_pit.contracts import ResearchTaskDraft
from money_pit.contracts import ResearchTaskMemory
from money_pit.contracts import ThesisMemory
from money_pit.contracts import UniverseLoader
from money_pit.graph.edges import require_predecessor
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import completed_with
from money_pit.graph.state import require_requested_as_of
from money_pit.graph.state import require_run_dir
from money_pit.graph.state import require_run_id
from money_pit.graph.state import require_run_started_at
from money_pit.pipeline.artifacts import StageArtifact
from money_pit.pipeline.artifacts import StageArtifactStore
from money_pit.pipeline.artifacts import persist_stage_artifact
from money_pit.pipeline.candidate_grounding import require_candidate_grounding
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.identity import stable_identifier
from money_pit.pipeline.temporal import require_model_temporal_authority
from money_pit.portfolio.universe import LayeredUniverse
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import bind_artifact_record
from money_pit.schemas.theses import CandidateThesis
from money_pit.storage.intelligence_work import CandidateDiscoveryOriginRecord
from money_pit.storage.intelligence_work import DiscoveryUnitKind


if TYPE_CHECKING:
    from money_pit.schemas.claims import CanonicalClaim
    from money_pit.schemas.claims import ClaimObservation
    from money_pit.schemas.universe import UniverseLayer
    from money_pit.storage.intelligence_work import DiscoveryBatchRecord
    from money_pit.storage.intelligence_work import IntelligenceWorkRepository
    from money_pit.storage.semantic_intelligence import SemanticIntelligenceRepository


class UnknownDiscoveryClaimError(Exception):
    """Raised when a candidate cites a claim outside the point-in-time input."""


class UnknownDiscoveryInstrumentError(Exception):
    """Raised when an agent silently introduces an instrument outside the universe."""


class AmbiguousResearchTaskCandidateError(Exception):
    """Raised when an A2 task cannot be bound to exactly one candidate."""


class UnknownResearchProviderError(Exception):
    """Raised before a task for a nonconfigured provider can be persisted."""


class DiscoveryPromptProjectionError(Exception):
    """Raised when one atomic A2 context unit cannot fit the request allowance."""


class DiscoveryArtifactPayload(BaseModel):
    """Immutable A2 audit payload."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    candidate_thesis_ids: tuple[str, ...]
    planned_research_task_ids: tuple[str, ...]
    research_queries: tuple[str, ...]
    requests: tuple[DiscoveryRequest, ...]
    response_candidate_count: int
    responses: tuple[DiscoveryDraft, ...]


_DISCOVERY_DRAFTS_ADAPTER: TypeAdapter[tuple[DiscoveryDraft, ...]] = TypeAdapter(tuple[DiscoveryDraft, ...])


def _claim_discovery_input(
    *,
    work_repository: IntelligenceWorkRepository | None,
    run_id: str,
    source_id: str | None,
    fallback_observation_ids: tuple[str, ...],
    clock: Callable[[], datetime],
) -> tuple[DiscoveryBatchRecord | None, tuple[str, ...], frozenset[str] | None]:
    if work_repository is None:
        return None, fallback_observation_ids, None
    created_at = clock()
    batch = work_repository.claim_discovery_batch(
        batch_id=stable_identifier("discovery-batch", {"run_id": run_id}),
        run_id=run_id,
        created_at=created_at,
        reclaim_before=created_at - timedelta(minutes=30),
        maximum_units=1,
        source_id=source_id,
    )
    if batch is None:
        return None, (), None
    units = work_repository.discovery_units_by_ids(batch.unit_ids)
    observation_ids = tuple(
        dict.fromkeys(
            identifier for unit in units for identifier in _payload_string_list(unit.payload, "observation_ids")
        )
    )
    universe_subjects = frozenset(unit.subject_id for unit in units if unit.kind is DiscoveryUnitKind.UNIVERSE_ENTRY)
    return batch, observation_ids, universe_subjects or None


def _resolve_discovery_drafts(
    *,
    requests: tuple[DiscoveryRequest, ...],
    discovery_batch: DiscoveryBatchRecord | None,
    agent: DiscoveryAgent,
    run_id: str,
) -> tuple[DiscoveryDraft, ...]:
    if discovery_batch is not None and discovery_batch.validated_output is not None:
        return _DISCOVERY_DRAFTS_ADAPTER.validate_python(discovery_batch.validated_output)
    drafts: list[DiscoveryDraft] = []
    for index, request in enumerate(requests):
        work_unit_id = f"discovery:{run_id}:{index}" if discovery_batch is None else discovery_batch.batch_id
        drafts.append(
            agent(
                request,
                context=InferenceInvocationContext(run_id=run_id, work_unit_id=work_unit_id),
            ).output
        )
    return tuple(drafts)


def _checkpoint_discovery_drafts(
    *,
    work_repository: IntelligenceWorkRepository | None,
    discovery_batch: DiscoveryBatchRecord | None,
    run_id: str,
    drafts: tuple[DiscoveryDraft, ...],
    candidates: tuple[CandidateThesis, ...],
    result_fingerprint: str,
) -> None:
    if work_repository is None or discovery_batch is None:
        return
    candidate_ids = tuple(item.candidate_thesis_id for item in candidates)
    if discovery_batch.validated_output is None:
        work_repository.checkpoint_discovery_output(
            batch_id=discovery_batch.batch_id,
            run_id=run_id,
            result_fingerprint=result_fingerprint,
            output_candidate_ids=candidate_ids,
            validated_output=[draft.model_dump(mode="json") for draft in drafts],
        )
        return
    if discovery_batch.output_candidate_ids != candidate_ids:
        raise DiscoveryPromptProjectionError(
            "Checkpointed discovery output does not reproduce its candidate identities"
        )


def _scope_discovery_universe(
    universe: LayeredUniverse,
    universe_subjects: frozenset[str] | None,
) -> LayeredUniverse:
    if universe_subjects is None:
        return universe
    return LayeredUniverse(
        candidates=tuple(candidate for candidate in universe.candidates if candidate.instrument in universe_subjects),
        observation_only=tuple(
            candidate for candidate in universe.observation_only if candidate.reference in universe_subjects
        ),
    )


def materialize_candidate(
    draft: CandidateThesisDraft,
    *,
    visible_claim_keys: frozenset[str],
    universe: LayeredUniverse,
    universe_origins: frozenset[tuple[UniverseLayer, str]],
    source_grounded_references: frozenset[str],
    known_at: datetime,
) -> CandidateThesis:
    """Assign deterministic candidate identity and enforce discovery authority."""
    missing_claims: set[str] = set(draft.discovery_basis.source_claim_keys) - visible_claim_keys
    if missing_claims:
        raise UnknownDiscoveryClaimError(f"Candidate cites claims not visible as_of: {sorted(missing_claims)}")
    if draft.discovery_basis.universe_layer is not None:
        origin = (draft.discovery_basis.universe_layer, draft.discovery_basis.universe_reference or "")
        if origin not in universe_origins:
            raise UnknownDiscoveryInstrumentError(f"Candidate cites an unknown universe origin: {origin}")
        if draft.instrument_reference is not None and _normalized_reference(
            draft.instrument_reference
        ) != _normalized_reference(origin[1]):
            raise UnknownDiscoveryInstrumentError(
                "Candidate reference does not match its authorized universe origin",
            )
    instrument_reference = None if draft.instrument_reference is None else draft.instrument_reference.strip()
    instrument = (
        None
        if instrument_reference is None
        else _resolve_candidate_instrument(
            instrument_reference,
            universe=universe,
            source_grounded_references=source_grounded_references,
            source_grounded=bool(draft.discovery_basis.source_claim_keys),
        )
    )
    candidate_id: str = stable_identifier(
        "candidate",
        {
            "subject": draft.subject.strip().casefold(),
            "direction": draft.direction.value,
            "instrument_reference": (None if instrument_reference is None else instrument_reference.casefold()),
            "instrument": instrument,
            "theme": draft.theme,
            "horizon_class": draft.horizon_class.value,
            "discovery_basis": draft.discovery_basis.model_dump(mode="json"),
        },
    )
    return CandidateThesis(
        candidate_thesis_id=candidate_id,
        subject=draft.subject,
        direction=draft.direction,
        instrument_reference=instrument_reference,
        instrument=instrument,
        theme=draft.theme,
        horizon_class=draft.horizon_class,
        discovery_basis=draft.discovery_basis,
        causal_mechanisms=draft.causal_mechanisms,
        regime_assumptions=draft.regime_assumptions,
        created_at=known_at,
        known_at=known_at,
    )


def _resolve_candidate_instrument(
    instrument_reference: str,
    *,
    universe: LayeredUniverse,
    source_grounded_references: frozenset[str],
    source_grounded: bool,
) -> str | None:
    """Resolve capital authority without treating ticker-shaped research text as tradable."""
    normalized = _normalized_reference(instrument_reference)
    if source_grounded and normalized not in source_grounded_references:
        raise UnknownDiscoveryInstrumentError(
            f"Candidate reference is not grounded by its cited source claims: {instrument_reference!r}",
        )
    resolved = {
        candidate.instrument
        for candidate in universe.candidates
        if normalized
        in {
            _normalized_reference(candidate.instrument),
            *(_normalized_reference(reference) for reference in candidate.references),
        }
    }
    if len(resolved) > 1:
        raise UnknownDiscoveryInstrumentError(
            f"Candidate reference resolves ambiguously in the layered universe: {instrument_reference!r}",
        )
    if resolved:
        return next(iter(resolved))
    observation_only = {_normalized_reference(candidate.reference) for candidate in universe.observation_only}
    if normalized in observation_only:
        return None
    if source_grounded:
        return None
    raise UnknownDiscoveryInstrumentError(
        f"Candidate reference is neither configured nor source-grounded: {instrument_reference!r}",
    )


def _normalized_reference(reference: str) -> str:
    """Return a comparison-only form while preserving original research text durably."""
    return " ".join(reference.split()).casefold()


def _source_grounded_references(
    draft: CandidateThesisDraft,
    request: DiscoveryRequest,
) -> frozenset[str]:
    """Return structured references carried by the exact claims cited by a candidate."""
    observation_ids = {
        observation_id
        for claim in request.claims
        if claim.canonical_claim_key in draft.discovery_basis.source_claim_keys
        for observation_id in claim.active_observation_ids
    }
    return frozenset(
        _normalized_reference(reference)
        for observation in request.observations
        if observation.observation_id in observation_ids
        for reference in (*observation.instruments, *observation.themes)
    )


def _require_chunk_candidate_grounding(
    candidates: tuple[CandidateThesis, ...],
    *,
    observations: tuple[ClaimObservation, ...],
    claims: tuple[CanonicalClaim, ...],
    observation_ids: tuple[str, ...],
) -> None:
    if not observation_ids:
        return
    for candidate in candidates:
        _ = require_candidate_grounding(
            candidate,
            observations=observations,
            claims=claims,
            eligible_observation_ids=frozenset(observation_ids),
        )


def bind_research_tasks(
    tasks: tuple[ResearchTaskDraft, ...],
    candidates: tuple[CandidateThesis, ...],
) -> tuple[ResearchTaskDraft, ...]:
    """Bind A2 task subjects to durable candidate identifiers."""
    candidates_by_subject: dict[str, list[CandidateThesis]] = {}
    for candidate in candidates:
        candidates_by_subject.setdefault(candidate.subject.strip().casefold(), []).append(candidate)
    bound: list[ResearchTaskDraft] = []
    candidate_ids: set[str] = {candidate.candidate_thesis_id for candidate in candidates}
    for task in tasks:
        candidate_id: str | None = task.candidate_thesis_id
        if candidate_id is not None and candidate_id in candidate_ids:
            bound.append(task)
            continue
        if task.candidate_subject is None:
            raise AmbiguousResearchTaskCandidateError("A2 research task must identify its candidate subject")
        matches: list[CandidateThesis] = candidates_by_subject.get(task.candidate_subject.strip().casefold(), [])
        if len(matches) != 1:
            raise AmbiguousResearchTaskCandidateError(
                f"Research task subject must resolve to exactly one candidate: {task.candidate_subject!r}",
            )
        bound.append(task.model_copy(update={"candidate_thesis_id": matches[0].candidate_thesis_id}))
    return tuple(bound)


def _required_semantic_repository(
    repository: SemanticIntelligenceRepository | None,
) -> SemanticIntelligenceRepository:
    if repository is None:
        raise ValueError("Incremental discovery requires semantic intelligence persistence")
    return repository


def _required_discovery_batch(batch: DiscoveryBatchRecord | None) -> DiscoveryBatchRecord:
    if batch is None:
        raise ValueError("Incremental discovery admission requires its claimed durable batch")
    return batch


def make_discovery_node(  # noqa: C901 - factory closes explicit typed A2 capabilities
    *,
    claims: ClaimMemory,
    theses: ThesisMemory,
    research_tasks: ResearchTaskMemory,
    load_universe: UniverseLoader,
    agent: DiscoveryAgent,
    implementation_version: str,
    artifact_store: StageArtifactStore,
    allowed_provider_names: tuple[str, ...],
    clock: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
    model_point_in_time_certified: bool = False,
    prompt_character_budget: int = 120_000,
    work_repository: IntelligenceWorkRepository | None = None,
    semantic_repository: SemanticIntelligenceRepository | None = None,
) -> PipelineNode:
    """Return A2 with claim-read, universe-read, and append-only candidate authority."""
    if work_repository is not None and semantic_repository is None:
        raise ValueError("Incremental discovery requires semantic intelligence persistence")
    incremental_semantic = semantic_repository
    request_budget = min(
        prompt_character_budget,
        request_character_allowance(agent, fallback=prompt_character_budget),
    )

    def node(state: PipelineState) -> PipelineState:
        require_predecessor(state, Stage.A2)
        run_id: str = require_run_id(state)
        requested_as_of: datetime = require_requested_as_of(state)
        model_context_at: datetime = clock()
        started_at: datetime = require_run_started_at(state)
        require_model_temporal_authority(
            requested_as_of=requested_as_of,
            run_started_at=started_at,
            requested_as_of_explicit=state.get("requested_as_of_explicit", False),
            point_in_time_certified=model_point_in_time_certified,
        )
        discovery_batch, same_run_ids, universe_subjects = _claim_discovery_input(
            work_repository=work_repository,
            run_id=run_id,
            source_id=state.get("source_id"),
            fallback_observation_ids=state.get("observation_ids", ()),
            clock=clock,
        )
        if work_repository is not None and discovery_batch is None:
            return {
                "completed_stages": completed_with(state, Stage.A2.value),
                "artifact_ids": state.get("artifact_ids", ()),
                "candidate_thesis_ids": (),
                "discovery_unit_ids": (),
                "decision_at": clock(),
            }
        projections = claims.projections_with_deltas(
            requested_as_of=requested_as_of,
            observation_ids=same_run_ids,
            resolution_ids=state.get("claim_resolution_decision_ids", ()),
            verification_ids=state.get("verification_result_ids", ()),
        )
        baseline_observations = claims.observations_as_of(as_of=requested_as_of)
        same_run_observations = claims.observations_by_ids(same_run_ids)
        if universe_subjects is not None:
            projections = ()
            observations = ()
        elif work_repository is None and state.get("source_id") is None:
            observations = tuple(
                {item.observation_id: item for item in (*baseline_observations, *same_run_observations)}.values()
            )
        else:
            same_run_set = frozenset(same_run_ids)
            projections = tuple(
                projection for projection in projections if same_run_set.intersection(projection.active_observation_ids)
            )
            visible_ids = (
                frozenset(
                    observation_id for projection in projections for observation_id in projection.active_observation_ids
                )
                | same_run_set
            )
            observations = tuple(
                item for item in (*baseline_observations, *same_run_observations) if item.observation_id in visible_ids
            )
        universe_subjects = _incremental_universe_subjects(
            work_repository=work_repository,
            universe_subjects=universe_subjects,
            observations=observations,
        )
        universe = _scope_discovery_universe(load_universe(requested_as_of), universe_subjects)
        request = DiscoveryRequest(
            universe=universe,
            claims=(),
            observations=(),
            signals=tuple(
                DiscoverySignal(
                    signal_id=projection.canonical_claim_key,
                    claim_key=projection.canonical_claim_key,
                    status=projection.current_status.value,
                    instruments=tuple(
                        dict.fromkeys(
                            instrument
                            for observation in observations
                            if observation.observation_id in projection.active_observation_ids
                            for instrument in observation.instruments
                        )
                    ),
                    themes=tuple(
                        dict.fromkeys(
                            theme
                            for observation in observations
                            if observation.observation_id in projection.active_observation_ids
                            for theme in observation.themes
                        )
                    ),
                    claim_texts=tuple(
                        observation.claim_text
                        for observation in observations
                        if observation.observation_id in projection.active_observation_ids
                    ),
                )
                for projection in projections
            ),
            requested_as_of=requested_as_of,
            context_known_at=model_context_at,
            allowed_provider_names=allowed_provider_names,
        )
        requests = _compact_discovery_request_chunks(request, request_budget)
        if len(requests) != 1:
            raise DiscoveryPromptProjectionError("One discovery update may issue only one model request")
        drafts = _resolve_discovery_drafts(
            requests=requests,
            discovery_batch=discovery_batch,
            agent=agent,
            run_id=run_id,
        )
        unknown_providers = {
            task.provider
            for draft in drafts
            for task in draft.research_tasks
            if task.provider not in frozenset(allowed_provider_names)
        }
        if unknown_providers:
            raise UnknownResearchProviderError(
                f"Discovery requested unconfigured providers: {sorted(unknown_providers)}"
            )
        decision_at = clock()
        visible_claim_keys: frozenset[str] = frozenset(projection.canonical_claim_key for projection in projections)
        materialized: list[CandidateThesis] = []
        tasks_by_chunk: list[tuple[tuple[ResearchTaskDraft, ...], tuple[CandidateThesis, ...]]] = []
        for bounded_request, draft in zip(requests, drafts, strict=True):
            authority_request = bounded_request.model_copy(update={"claims": projections, "observations": observations})
            chunk_candidates = tuple(
                materialize_candidate(
                    candidate,
                    visible_claim_keys=frozenset(signal.claim_key for signal in bounded_request.signals),
                    universe=bounded_request.universe,
                    universe_origins=_universe_origins(bounded_request.universe),
                    source_grounded_references=_source_grounded_references(candidate, authority_request),
                    known_at=clock(),
                )
                for candidate in draft.candidates
            )
            _require_chunk_candidate_grounding(
                chunk_candidates,
                observations=observations,
                claims=projections,
                observation_ids=same_run_ids,
            )
            materialized.extend(chunk_candidates)
            tasks_by_chunk.append((draft.research_tasks, chunk_candidates))
        candidates = tuple({item.candidate_thesis_id: item for item in materialized}.values())
        bound_tasks = tuple(
            {
                task.model_dump_json(): task
                for task_drafts, chunk_candidates in tasks_by_chunk
                for task in bind_research_tasks(task_drafts, chunk_candidates)
            }.values()
        )
        result_fingerprint = hashlib.sha256(
            json.dumps(
                [draft.model_dump(mode="json") for draft in drafts],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        _checkpoint_discovery_drafts(
            work_repository=work_repository,
            discovery_batch=discovery_batch,
            run_id=run_id,
            drafts=drafts,
            candidates=candidates,
            result_fingerprint=result_fingerprint,
        )
        for candidate in candidates:
            if work_repository is None:
                theses.append_candidate(candidate)
            else:
                claimed_batch = _required_discovery_batch(discovery_batch)
                _ = _required_semantic_repository(incremental_semantic).admit_candidate_semantics(
                    candidate,
                    batch_id=claimed_batch.batch_id,
                    origin_unit_ids=claimed_batch.unit_ids,
                    recorded_at=clock(),
                )
        planned_research_task_ids = tuple(
            research_tasks.append_task(
                task,
                run_id=run_id,
                known_at=clock(),
                origin_unit_ids=(() if discovery_batch is None else discovery_batch.unit_ids),
            )
            for task in bound_tasks
        )
        if work_repository is not None and discovery_batch is not None:
            work_repository.complete_discovery_batch(
                batch_id=discovery_batch.batch_id,
                run_id=run_id,
                completed_at=clock(),
                result_fingerprint=result_fingerprint,
                output_candidate_ids=tuple(item.candidate_thesis_id for item in candidates),
                candidate_origins=tuple(
                    CandidateDiscoveryOriginRecord(
                        candidate_thesis_id=candidate.candidate_thesis_id,
                        unit_id=unit_id,
                    )
                    for candidate in candidates
                    for unit_id in discovery_batch.unit_ids
                ),
            )

        payload = DiscoveryArtifactPayload(
            candidate_thesis_ids=tuple(candidate.candidate_thesis_id for candidate in candidates),
            planned_research_task_ids=planned_research_task_ids,
            research_queries=tuple(task.query for task in bound_tasks),
            requests=requests,
            response_candidate_count=sum(len(draft.candidates) for draft in drafts),
            responses=drafts,
        )
        artifact: StageArtifact = persist_stage_artifact(
            require_run_dir(state),
            run_id=run_id,
            stage=Stage.A2,
            requested_as_of=requested_as_of,
            started_at=started_at,
            known_at=clock(),
            decision_at=decision_at,
            input_ids=tuple(
                bind_artifact_record(ArtifactRecordKind.CANONICAL_CLAIM, identifier)
                for identifier in sorted(visible_claim_keys)
            ),
            output_ids=(
                *(
                    bind_artifact_record(ArtifactRecordKind.CANDIDATE_THESIS, identifier)
                    for identifier in payload.candidate_thesis_ids
                ),
                *(
                    bind_artifact_record(ArtifactRecordKind.PLANNED_RESEARCH_TASK, identifier)
                    for identifier in payload.planned_research_task_ids
                ),
            ),
            implementation_version=implementation_version,
            payload=payload,
            artifact_store=artifact_store,
        )
        return {
            "completed_stages": completed_with(state, Stage.A2.value),
            "artifact_ids": (*state.get("artifact_ids", ()), artifact.artifact_id),
            "candidate_thesis_ids": payload.candidate_thesis_ids,
            "discovery_unit_ids": () if discovery_batch is None else discovery_batch.unit_ids,
            "decision_at": decision_at,
        }

    return node


def _incremental_universe_subjects(
    *,
    work_repository: IntelligenceWorkRepository | None,
    universe_subjects: frozenset[str] | None,
    observations: tuple[ClaimObservation, ...],
) -> frozenset[str] | None:
    """Restrict incremental discovery to instruments named by claimed work."""
    if work_repository is None or universe_subjects is not None:
        return universe_subjects
    return frozenset(instrument for observation in observations for instrument in observation.instruments)


def _compact_discovery_request_chunks(
    request: DiscoveryRequest,
    character_budget: int,
) -> tuple[DiscoveryRequest, ...]:
    """Pack compact discovery signals without replaying the historical claim store."""
    if serialized_inference_request_size(request) > character_budget:
        raise DiscoveryPromptProjectionError("Oldest discovery work unit exceeds the inference request allowance")
    return (request,)


def _payload_string_list(payload: JsonValue, key: str) -> tuple[str, ...]:
    """Validate one compact list from a durable discovery payload."""
    if not isinstance(payload, dict):
        raise DiscoveryPromptProjectionError("Discovery work payload must be an object")
    value: JsonValue | None = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DiscoveryPromptProjectionError(f"Discovery work payload has invalid {key}")
    return tuple(item for item in value if isinstance(item, str))


def _universe_origins(universe: LayeredUniverse) -> frozenset[tuple[UniverseLayer, str]]:
    """Return exact layer/reference pairs authorized by deterministic universe inputs."""
    tradable = {
        (layer, reference)
        for candidate in universe.candidates
        for layer in candidate.layers
        for reference in candidate.references
    }
    observation_only = {(candidate.layer, candidate.reference) for candidate in universe.observation_only}
    return frozenset(tradable | observation_only)
