"""Module implementing A1 evidence interpretation and claim persistence."""

from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.agents.budget import inference_request_fits
from money_pit.contracts import ClaimObservationDraft
from money_pit.contracts import EvidencePromptRecord
from money_pit.contracts import EvidenceWorkRepository
from money_pit.contracts import InterpretationAgent
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import InterpretationRequest
from money_pit.evidence.aliases import EvidenceAliasProjection
from money_pit.evidence.aliases import EvidenceProjectionChunk
from money_pit.evidence.aliases import project_evidence
from money_pit.evidence.work import EvidenceInterpretationWork
from money_pit.graph.edges import require_predecessor
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import completed_with
from money_pit.graph.state import require_requested_as_of
from money_pit.graph.state import require_run_dir
from money_pit.graph.state import require_run_id
from money_pit.graph.state import require_run_started_at
from money_pit.pipeline.artifacts import StageArtifact
from money_pit.pipeline.artifacts import build_stage_artifact
from money_pit.pipeline.artifacts import try_install_stage_artifact_file
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.identity import normalized_claim_text
from money_pit.pipeline.identity import stable_identifier
from money_pit.pipeline.temporal import require_model_temporal_authority
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import bind_artifact_record
from money_pit.storage.admission import IntelligenceAdmissionRepository
from money_pit.storage.admission import InterpretationAdmission


_DEFAULT_DOCUMENT_LIMIT = 100
_DEFAULT_PROMPT_CHARACTER_BUDGET = 120_000


class FutureEvidenceError(Exception):
    """Raised when pending evidence violates the run's point-in-time boundary."""


class FutureClaimAssertionError(Exception):
    """Raised when an interpretation asserts knowledge after the run boundary."""


class InterpretationArtifactPayload(BaseModel):
    """Immutable A1 audit payload."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    source_item_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    attempt_ids: tuple[str, ...]
    fragment_ids: tuple[str, ...]
    requests: tuple[InterpretationRequest, ...]
    responses: tuple[InterpretationDraft, ...]
    alias_maps: tuple[dict[str, tuple[str, ...]], ...]


class InterpretationOutcome(BaseModel):
    """Durable trace returned by explicit-document interpretation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    attempt_id: str
    document_id: str
    fragment_ids: tuple[str, ...]
    observations: tuple[ClaimObservation, ...]
    requests: tuple[InterpretationRequest, ...]
    responses: tuple[InterpretationDraft, ...]
    alias_maps: tuple[dict[str, tuple[str, ...]], ...]
    known_at: datetime
    completed_at: datetime


def materialize_observation(
    draft: ClaimObservationDraft,
    *,
    source_item_id: str,
    projection: EvidenceAliasProjection,
    known_at: datetime,
    assertion_boundary: datetime,
    economic_boundary: datetime | None = None,
) -> ClaimObservation:
    """Resolve aliases and assign deterministic claim identities."""
    if draft.asserted_at > assertion_boundary:
        raise FutureClaimAssertionError("Claim asserted_at cannot be after the decision boundary")
    if economic_boundary is not None and draft.asserted_at > economic_boundary:
        raise FutureClaimAssertionError(
            "Historical interpretation cannot assert source knowledge after requested_as_of",
        )
    normalized_text: str = normalized_claim_text(draft.claim_text)
    evidence_ids: tuple[str, ...] = projection.resolve(draft.evidence_aliases)
    observation_id: str = stable_identifier(
        "observation",
        {
            "source_item_id": source_item_id,
            "claim_text": normalized_text,
            "asserted_at": draft.asserted_at.isoformat(),
            "evidence_fragment_ids": list(evidence_ids),
        },
    )
    return ClaimObservation(
        observation_id=observation_id,
        claim_text=draft.claim_text,
        claim_kind=draft.claim_kind,
        category=draft.category,
        source_item_id=source_item_id,
        evidence_fragment_ids=evidence_ids,
        asserted_at=draft.asserted_at,
        known_at=known_at,
        effective_from=draft.effective_from,
        event_at=draft.event_at,
        review_at=draft.review_at,
        valid_until=draft.valid_until,
        horizon_class=draft.horizon_class,
        instruments=tuple(instrument.strip().upper() for instrument in draft.instruments),
        themes=draft.themes,
        causal_mechanisms=draft.causal_mechanisms,
        regime_assumptions=draft.regime_assumptions,
        supersedes_observation_id=draft.supersedes_observation_id,
    )


def interpret_document(
    document: EvidenceDocument,
    *,
    agent: InterpretationAgent,
    requested_as_of: datetime,
    decision_at: datetime,
    known_at: datetime,
    character_budget: int,
    baseline_only: bool = True,
) -> tuple[ClaimObservation, ...]:
    """Interpret every bounded evidence chunk and resolve only shown aliases."""
    if baseline_only and document.asset.retrieved_at > requested_as_of:
        raise FutureEvidenceError("Evidence retrieved after the run as_of boundary is not visible")
    projection: EvidenceAliasProjection = project_evidence(document.fragments)
    observations: list[ClaimObservation] = []

    def request_for(chunk: EvidenceProjectionChunk) -> InterpretationRequest:
        return InterpretationRequest(
            source_item_id=document.asset.source_item_id,
            evidence=tuple(
                EvidencePromptRecord(
                    alias=item.alias,
                    kind=item.kind,
                    text=item.text,
                    core=item.alias in chunk.core_aliases,
                )
                for item in chunk.evidence
            ),
            requested_as_of=requested_as_of,
            context_known_at=decision_at,
        )

    for chunk in projection.chunks_fitting(
        lambda value: inference_request_fits(agent, request_for(value), fallback=character_budget)
    ):
        request = request_for(chunk)
        draft = agent(request)
        chunk_projection = EvidenceAliasProjection(evidence=chunk.evidence)
        observations.extend(
            materialize_observation(
                observation,
                source_item_id=document.asset.source_item_id,
                projection=chunk_projection,
                known_at=known_at,
                assertion_boundary=decision_at,
                economic_boundary=requested_as_of if baseline_only else None,
            )
            for observation in draft.observations
            if _earliest_alias_is_core(observation, chunk_projection, chunk.core_aliases)
        )
    unique: dict[str, ClaimObservation] = {item.observation_id: item for item in observations}
    return tuple(unique[key] for key in sorted(unique))


class InterpretationService:
    """Interpret one exact evidence acquisition with durable attempt bookkeeping."""

    def __init__(
        self,
        *,
        evidence: EvidenceWorkRepository,
        admission: IntelligenceAdmissionRepository,
        agent: InterpretationAgent,
        implementation_version: str,
        character_budget: int = _DEFAULT_PROMPT_CHARACTER_BUDGET,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
    ) -> None:
        """Bind exact work persistence, atomic admission, and model execution."""
        self._evidence: EvidenceWorkRepository = evidence
        self._admission: IntelligenceAdmissionRepository = admission
        self._agent: InterpretationAgent = agent
        self._implementation_version: str = implementation_version
        self._character_budget: int = character_budget
        self._clock: Callable[[], datetime] = clock

    def interpret_pending(
        self,
        work: EvidenceInterpretationWork,
        *,
        run_id: str,
        requested_as_of: datetime,
        context_known_at: datetime,
        baseline_only: bool,
    ) -> InterpretationOutcome:
        """Interpret one exact work item while leaving successful admission pending."""
        attempt_id = self._evidence.begin_interpretation(
            work,
            run_id=run_id,
            interpreter_version=self._implementation_version,
            started_at=self._clock(),
        )
        document = work.document
        requests: list[InterpretationRequest] = []
        responses: list[InterpretationDraft] = []
        alias_maps: list[dict[str, tuple[str, ...]]] = []
        observations: list[ClaimObservation] = []
        try:
            if baseline_only and document.asset.retrieved_at > requested_as_of:
                raise FutureEvidenceError("Evidence retrieved after the requested boundary is not visible")
            projection = project_evidence(document.fragments)

            def request_for(chunk: EvidenceProjectionChunk) -> InterpretationRequest:
                return InterpretationRequest(
                    source_item_id=document.asset.source_item_id,
                    evidence=tuple(
                        EvidencePromptRecord(
                            alias=item.alias,
                            kind=item.kind,
                            text=item.text,
                            core=item.alias in chunk.core_aliases,
                        )
                        for item in chunk.evidence
                    ),
                    requested_as_of=requested_as_of,
                    context_known_at=context_known_at,
                )

            for chunk in projection.chunks_fitting(
                lambda value: inference_request_fits(
                    self._agent,
                    request_for(value),
                    fallback=self._character_budget,
                )
            ):
                request = request_for(chunk)
                response = self._agent(request)
                chunk_projection = EvidenceAliasProjection(evidence=chunk.evidence)
                decision_completed_at = self._clock()
                known_at = decision_completed_at
                observations.extend(
                    materialize_observation(
                        draft,
                        source_item_id=document.asset.source_item_id,
                        projection=chunk_projection,
                        known_at=known_at,
                        assertion_boundary=decision_completed_at,
                        economic_boundary=requested_as_of if baseline_only else None,
                    )
                    for draft in response.observations
                    if _earliest_alias_is_core(draft, chunk_projection, chunk.core_aliases)
                )
                requests.append(request)
                responses.append(response)
                alias_maps.append({item.alias: item.fragment_ids for item in chunk.evidence})
            unique = {item.observation_id: item for item in observations}
            persisted = tuple(unique[key] for key in sorted(unique))
        except Exception as error:
            self._evidence.fail_interpretation(
                attempt_id,
                failure_kind=type(error).__name__,
                completed_at=self._clock(),
            )
            raise
        completed_at = self._clock()
        return InterpretationOutcome(
            attempt_id=attempt_id,
            document_id=document.asset.asset_id,
            fragment_ids=tuple(fragment.fragment_id for fragment in document.fragments),
            observations=persisted,
            requests=tuple(requests),
            responses=tuple(responses),
            alias_maps=tuple(alias_maps),
            known_at=completed_at,
            completed_at=completed_at,
        )

    def interpret(
        self,
        work: EvidenceInterpretationWork,
        *,
        run_id: str,
        requested_as_of: datetime,
        context_known_at: datetime,
        baseline_only: bool,
    ) -> InterpretationOutcome:
        """Interpret and atomically admit one A3 evidence acquisition."""
        outcome = self.interpret_pending(
            work,
            run_id=run_id,
            requested_as_of=requested_as_of,
            context_known_at=context_known_at,
            baseline_only=baseline_only,
        )
        try:
            self._admission.admit_research_interpretation(
                InterpretationAdmission(
                    attempt_id=outcome.attempt_id,
                    observations=outcome.observations,
                ),
                completed_at=outcome.completed_at,
                known_at=outcome.known_at,
                run_id=run_id,
            )
        except Exception as error:
            self.fail_pending(outcome, failure_kind=type(error).__name__)
            raise
        return outcome

    def fail_pending(self, outcome: InterpretationOutcome, *, failure_kind: str) -> None:
        """Terminate an interpreted but unadmitted attempt after a batch failure."""
        self._evidence.fail_interpretation(
            outcome.attempt_id,
            failure_kind=failure_kind,
            completed_at=self._clock(),
        )


def make_interpretation_node(
    *,
    evidence: EvidenceWorkRepository,
    admission: IntelligenceAdmissionRepository,
    agent: InterpretationAgent,
    implementation_version: str,
    document_limit: int = _DEFAULT_DOCUMENT_LIMIT,
    prompt_character_budget: int = _DEFAULT_PROMPT_CHARACTER_BUDGET,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
    model_point_in_time_certified: bool = False,
    interpretation_service: InterpretationService | None = None,
) -> PipelineNode:
    """Return A1 with only evidence-read and claim-append capabilities."""
    service = interpretation_service or InterpretationService(
        evidence=evidence,
        admission=admission,
        agent=agent,
        implementation_version=implementation_version,
        character_budget=prompt_character_budget,
        clock=clock,
    )

    def node(state: PipelineState) -> PipelineState:
        require_predecessor(state, Stage.A1)
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
        work_items = evidence.list_pending_documents(
            as_of=requested_as_of,
            source_id=state.get("source_id"),
            limit=document_limit,
            interpreter_version=implementation_version,
        )
        outcomes: list[InterpretationOutcome] = []
        source_item_ids: list[str] = []
        try:
            for work in work_items:
                document: EvidenceDocument = work.document
                outcome = service.interpret_pending(
                    work,
                    run_id=run_id,
                    requested_as_of=requested_as_of,
                    context_known_at=model_context_at,
                    baseline_only=True,
                )
                outcomes.append(outcome)
                source_item_ids.append(document.asset.source_item_id)
        except Exception as error:
            for outcome in outcomes:
                service.fail_pending(outcome, failure_kind=type(error).__name__)
            raise

        decision_at = clock()

        payload = InterpretationArtifactPayload(
            source_item_ids=tuple(source_item_ids),
            observation_ids=tuple(item.observation_id for outcome in outcomes for item in outcome.observations),
            attempt_ids=tuple(outcome.attempt_id for outcome in outcomes),
            fragment_ids=tuple(
                dict.fromkeys(fragment_id for outcome in outcomes for fragment_id in outcome.fragment_ids)
            ),
            requests=tuple(request for outcome in outcomes for request in outcome.requests),
            responses=tuple(response for outcome in outcomes for response in outcome.responses),
            alias_maps=tuple(alias_map for outcome in outcomes for alias_map in outcome.alias_maps),
        )
        known_at = clock()
        artifact: StageArtifact = build_stage_artifact(
            run_id=run_id,
            stage=Stage.A1,
            requested_as_of=requested_as_of,
            started_at=started_at,
            known_at=known_at,
            decision_at=decision_at,
            input_ids=tuple(
                dict.fromkeys(
                    (
                        *(
                            binding
                            for work in work_items
                            for binding in (
                                bind_artifact_record(
                                    ArtifactRecordKind.SOURCE_ITEM_VERSION,
                                    f"{work.document.asset.source_item_id}@{work.content_version}",
                                ),
                                bind_artifact_record(
                                    ArtifactRecordKind.ASSET,
                                    work.document.asset.asset_id,
                                ),
                                bind_artifact_record(
                                    ArtifactRecordKind.DOCUMENT,
                                    work.document.asset.asset_id,
                                ),
                            )
                        ),
                        *(
                            bind_artifact_record(ArtifactRecordKind.FRAGMENT, fragment_id)
                            for fragment_id in payload.fragment_ids
                        ),
                    )
                )
            ),
            output_ids=(
                *(
                    bind_artifact_record(ArtifactRecordKind.INTERPRETATION_ATTEMPT, identifier)
                    for identifier in payload.attempt_ids
                ),
                *(
                    bind_artifact_record(ArtifactRecordKind.OBSERVATION, identifier)
                    for identifier in payload.observation_ids
                ),
            ),
            implementation_version=implementation_version,
            payload=payload,
        )
        try:
            admission.admit_interpretation(
                tuple(
                    InterpretationAdmission(
                        attempt_id=outcome.attempt_id,
                        observations=outcome.observations,
                    )
                    for outcome in outcomes
                ),
                completed_at=known_at,
                known_at=known_at,
                artifact=artifact,
            )
        except Exception as error:
            for outcome in outcomes:
                service.fail_pending(outcome, failure_kind=type(error).__name__)
            raise
        _ = try_install_stage_artifact_file(require_run_dir(state), artifact)
        return {
            "completed_stages": completed_with(state, Stage.A1.value),
            "artifact_ids": (*state.get("artifact_ids", ()), artifact.artifact_id),
            "observation_ids": (*state.get("observation_ids", ()), *payload.observation_ids),
            "evidence_fragment_ids": (*state.get("evidence_fragment_ids", ()), *payload.fragment_ids),
            "interpretation_attempt_ids": (*state.get("interpretation_attempt_ids", ()), *payload.attempt_ids),
            "decision_at": decision_at,
        }

    return node


def _earliest_alias_is_core(
    draft: ClaimObservationDraft,
    projection: EvidenceAliasProjection,
    core_aliases: frozenset[str],
) -> bool:
    """Accept a chunk claim only when its earliest cited alias belongs to that chunk."""
    order = {item.alias: index for index, item in enumerate(projection.evidence)}
    try:
        earliest = min(draft.evidence_aliases, key=order.__getitem__)
    except KeyError as error:
        _ = projection.resolve(draft.evidence_aliases)
        raise AssertionError("Alias resolution unexpectedly returned") from error
    return earliest in core_aliases
