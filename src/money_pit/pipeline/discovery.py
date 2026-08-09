"""Module implementing A2 layered thesis discovery and research planning."""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003 - used by the runtime default clock
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.agents.budget import request_character_allowance
from money_pit.agents.budget import serialized_inference_request_size
from money_pit.contracts import CandidateThesisDraft
from money_pit.contracts import ClaimMemory
from money_pit.contracts import DiscoveryAgent
from money_pit.contracts import DiscoveryDraft
from money_pit.contracts import DiscoveryRequest
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
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.identity import stable_identifier
from money_pit.pipeline.temporal import require_model_temporal_authority
from money_pit.portfolio.universe import LayeredUniverse
from money_pit.portfolio.universe import ObservationOnlyCandidate
from money_pit.portfolio.universe import UniverseCandidate
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import bind_artifact_record
from money_pit.schemas.theses import CandidateThesis


if TYPE_CHECKING:
    from money_pit.schemas.claims import CanonicalClaim
    from money_pit.schemas.claims import ClaimObservation
    from money_pit.schemas.universe import UniverseLayer


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


@dataclass(frozen=True)
class _DiscoveryProjectionUnit:
    identifier: str
    candidates: tuple[UniverseCandidate, ...] = ()
    observation_only: tuple[ObservationOnlyCandidate, ...] = ()
    claims: tuple[CanonicalClaim, ...] = ()
    observations: tuple[ClaimObservation, ...] = ()


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


def make_discovery_node(
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
) -> PipelineNode:
    """Return A2 with claim-read, universe-read, and append-only candidate authority."""
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
        same_run_ids = state.get("observation_ids", ())
        projections = claims.projections_with_deltas(
            requested_as_of=requested_as_of,
            observation_ids=same_run_ids,
            resolution_ids=state.get("claim_resolution_decision_ids", ()),
            verification_ids=state.get("verification_result_ids", ()),
        )
        baseline_observations = claims.observations_as_of(as_of=requested_as_of)
        same_run_observations = claims.observations_by_ids(same_run_ids)
        observations = tuple(
            {item.observation_id: item for item in (*baseline_observations, *same_run_observations)}.values()
        )
        universe = load_universe(requested_as_of)
        request = DiscoveryRequest(
            universe=universe,
            claims=projections,
            observations=observations,
            requested_as_of=requested_as_of,
            context_known_at=model_context_at,
            allowed_provider_names=allowed_provider_names,
        )
        requests = _discovery_request_chunks(request, request_budget)
        drafts = tuple(agent(item) for item in requests)
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
            chunk_candidates = tuple(
                materialize_candidate(
                    candidate,
                    visible_claim_keys=frozenset(claim.canonical_claim_key for claim in bounded_request.claims),
                    universe=bounded_request.universe,
                    universe_origins=_universe_origins(bounded_request.universe),
                    source_grounded_references=_source_grounded_references(candidate, bounded_request),
                    known_at=clock(),
                )
                for candidate in draft.candidates
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
        for candidate in candidates:
            theses.append_candidate(candidate)
        planned_research_task_ids = tuple(
            research_tasks.append_task(task, run_id=run_id, known_at=clock()) for task in bound_tasks
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
            "decision_at": decision_at,
        }

    return node


def _discovery_request_chunks(
    request: DiscoveryRequest,
    character_budget: int,
) -> tuple[DiscoveryRequest, ...]:
    """Greedily cover the complete universe and claim context in atomic units."""
    observations_by_id = {item.observation_id: item for item in request.observations}
    universe_by_instrument = {item.instrument: item for item in request.universe.candidates}
    claimed_observation_ids = {
        observation_id for claim in request.claims for observation_id in claim.active_observation_ids
    }
    units = (
        *(
            _DiscoveryProjectionUnit(
                identifier=f"universe:{item.instrument}",
                candidates=(item,),
            )
            for item in request.universe.candidates
        ),
        *(
            _DiscoveryProjectionUnit(
                identifier=f"universe-observation:{item.layer.value}:{item.reference}",
                observation_only=(item,),
            )
            for item in request.universe.observation_only
        ),
        *(
            _DiscoveryProjectionUnit(
                identifier=f"claim:{item.canonical_claim_key}",
                candidates=tuple(
                    universe_by_instrument[instrument]
                    for instrument in dict.fromkeys(
                        instrument
                        for observation_id in item.active_observation_ids
                        if observation_id in observations_by_id
                        for instrument in observations_by_id[observation_id].instruments
                    )
                    if instrument in universe_by_instrument
                ),
                claims=(item,),
                observations=tuple(
                    observations_by_id[observation_id]
                    for observation_id in item.active_observation_ids
                    if observation_id in observations_by_id
                ),
            )
            for item in request.claims
        ),
        *(
            _DiscoveryProjectionUnit(
                identifier=f"observation:{item.observation_id}",
                observations=(item,),
            )
            for item in request.observations
            if item.observation_id not in claimed_observation_ids
        ),
    )
    all_ids = tuple(unit.identifier for unit in units)
    empty = request.model_copy(
        update={
            "universe": LayeredUniverse(candidates=(), observation_only=()),
            "claims": (),
            "observations": (),
            "omitted_input_ids": all_ids,
        },
    )
    if not units:
        if serialized_inference_request_size(empty) > character_budget:
            raise DiscoveryPromptProjectionError(
                "Discovery fixed context exceeds the inference request allowance",
            )
        return (empty,)

    chunks: list[DiscoveryRequest] = []
    current_units: list[_DiscoveryProjectionUnit] = []
    for unit in units:
        trial = _discovery_chunk(request, (*current_units, unit), all_ids)
        if serialized_inference_request_size(trial) <= character_budget:
            current_units.append(unit)
            continue
        atomic = _discovery_chunk(request, (unit,), all_ids)
        if serialized_inference_request_size(atomic) > character_budget:
            raise DiscoveryPromptProjectionError(
                f"Atomic discovery context exceeds inference allowance: {unit.identifier}",
            )
        if current_units:
            chunks.append(_discovery_chunk(request, tuple(current_units), all_ids))
        current_units = [unit]
    if current_units:
        chunks.append(_discovery_chunk(request, tuple(current_units), all_ids))
    return tuple(chunks)


def _discovery_chunk(
    request: DiscoveryRequest,
    units: tuple[_DiscoveryProjectionUnit, ...],
    all_ids: tuple[str, ...],
) -> DiscoveryRequest:
    included = {unit.identifier for unit in units}
    observations = {item.observation_id: item for unit in units for item in unit.observations}
    return request.model_copy(
        update={
            "universe": LayeredUniverse(
                candidates=tuple(item for unit in units for item in unit.candidates),
                observation_only=tuple(item for unit in units for item in unit.observation_only),
            ),
            "claims": tuple(item for unit in units for item in unit.claims),
            "observations": tuple(observations.values()),
            "omitted_input_ids": tuple(identifier for identifier in all_ids if identifier not in included),
        },
    )


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
