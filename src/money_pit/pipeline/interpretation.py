"""Module implementing A1 evidence interpretation and claim persistence."""

from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import JsonValue
from pydantic import TypeAdapter

from money_pit.agents.budget import inference_request_fits
from money_pit.agents.inference import InferenceInvocationContext
from money_pit.contracts import ClaimObservationDraft
from money_pit.contracts import EvidencePromptRecord
from money_pit.contracts import EvidenceWorkRepository
from money_pit.contracts import InterpretationAgent
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import InterpretationRequest
from money_pit.evidence.aliases import AliasedEvidence
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
from money_pit.pipeline.identity import interpretation_bundle_id
from money_pit.pipeline.identity import normalized_claim_text
from money_pit.pipeline.identity import ordered_work_fingerprint
from money_pit.pipeline.identity import stable_identifier
from money_pit.pipeline.temporal import require_model_temporal_authority
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import bind_artifact_record
from money_pit.storage.admission import IntelligenceAdmissionRepository
from money_pit.storage.admission import InterpretationAdmission
from money_pit.storage.intelligence_work import DiscoveryOriginRecord
from money_pit.storage.intelligence_work import DiscoveryUnitKind
from money_pit.storage.intelligence_work import DiscoveryUnitRecord
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import InterpretationBundleRecord
from money_pit.storage.intelligence_work import InterpretationChunkSpec


_DEFAULT_DOCUMENT_LIMIT = 100
_DEFAULT_PROMPT_CHARACTER_BUDGET = 120_000
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


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
    context: InferenceInvocationContext,
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
        draft = agent(request, context=context).output
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
                response = self._agent(
                    request,
                    context=InferenceInvocationContext(run_id=run_id, work_unit_id=attempt_id),
                ).output
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

    def interpret_bundle_pending(
        self,
        work_items: tuple[EvidenceInterpretationWork, ...],
        *,
        run_id: str,
        requested_as_of: datetime,
        context_known_at: datetime,
        baseline_only: bool,
    ) -> tuple[InterpretationOutcome, ...]:
        """Interpret one source-item version as a shared semantic bundle."""
        if not work_items:
            return ()
        first = work_items[0]
        bundle_key = (first.document.asset.source_item_id, first.content_version)
        if any((item.document.asset.source_item_id, item.content_version) != bundle_key for item in work_items):
            raise ValueError("Interpretation bundle must contain one source-item version")
        attempt_ids = tuple(
            self._evidence.begin_interpretation(
                item,
                run_id=run_id,
                interpreter_version=self._implementation_version,
                started_at=self._clock(),
            )
            for item in work_items
        )
        combined = EvidenceInterpretationWork(
            document=first.document.model_copy(
                update={
                    "fragments": tuple(fragment for item in work_items for fragment in item.document.fragments),
                },
            ),
            content_version=first.content_version,
        )
        try:
            primary = self._interpret_started_work(
                combined,
                attempt_id=attempt_ids[0],
                run_id=run_id,
                requested_as_of=requested_as_of,
                context_known_at=context_known_at,
                baseline_only=baseline_only,
            )
        except Exception as error:
            for attempt_id in attempt_ids:
                self._evidence.fail_interpretation(
                    attempt_id,
                    failure_kind=type(error).__name__,
                    completed_at=self._clock(),
                )
            raise
        return (
            primary,
            *(
                InterpretationOutcome(
                    attempt_id=attempt_id,
                    document_id=item.document.asset.asset_id,
                    fragment_ids=tuple(fragment.fragment_id for fragment in item.document.fragments),
                    observations=(),
                    requests=(),
                    responses=(),
                    alias_maps=(),
                    known_at=primary.known_at,
                    completed_at=primary.completed_at,
                )
                for item, attempt_id in zip(work_items[1:], attempt_ids[1:], strict=True)
            ),
        )

    def _interpret_started_work(
        self,
        work: EvidenceInterpretationWork,
        *,
        attempt_id: str,
        run_id: str,
        requested_as_of: datetime,
        context_known_at: datetime,
        baseline_only: bool,
    ) -> InterpretationOutcome:
        """Interpret work whose durable attempt already exists."""
        document = work.document
        if baseline_only and document.asset.retrieved_at > requested_as_of:
            raise FutureEvidenceError("Evidence retrieved after the requested boundary is not visible")
        projection = project_evidence(document.fragments)
        requests: list[InterpretationRequest] = []
        responses: list[InterpretationDraft] = []
        alias_maps: list[dict[str, tuple[str, ...]]] = []
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
                context_known_at=context_known_at,
            )

        for chunk in projection.chunks_fitting(
            lambda value: inference_request_fits(self._agent, request_for(value), fallback=self._character_budget)
        ):
            request = request_for(chunk)
            response = self._agent(
                request,
                context=InferenceInvocationContext(run_id=run_id, work_unit_id=attempt_id),
            ).output
            chunk_projection = EvidenceAliasProjection(evidence=chunk.evidence)
            completed_at = self._clock()
            observations.extend(
                materialize_observation(
                    draft,
                    source_item_id=document.asset.source_item_id,
                    projection=chunk_projection,
                    known_at=completed_at,
                    assertion_boundary=completed_at,
                    economic_boundary=requested_as_of if baseline_only else None,
                )
                for draft in response.observations
                if _earliest_alias_is_core(draft, chunk_projection, chunk.core_aliases)
            )
            requests.append(request)
            responses.append(response)
            alias_maps.append({item.alias: item.fragment_ids for item in chunk.evidence})
        completed_at = self._clock()
        unique = {item.observation_id: item for item in observations}
        return InterpretationOutcome(
            attempt_id=attempt_id,
            document_id=document.asset.asset_id,
            fragment_ids=tuple(fragment.fragment_id for fragment in document.fragments),
            observations=tuple(unique[key] for key in sorted(unique)),
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
        reusable = self._evidence.reusable_interpretation(
            work,
            interpreter_version=self._implementation_version,
        )
        if reusable is not None:
            return InterpretationOutcome(
                attempt_id=reusable.attempt_id,
                document_id=reusable.document_id,
                fragment_ids=reusable.fragment_ids,
                observations=reusable.observations,
                requests=(),
                responses=(),
                alias_maps=(),
                known_at=reusable.known_at,
                completed_at=reusable.completed_at,
            )
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
    work_repository: IntelligenceWorkRepository | None = None,
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
        if work_repository is not None:
            return _run_incremental_interpretation(
                state,
                work_items=work_items,
                evidence=evidence,
                admission=admission,
                agent=agent,
                implementation_version=implementation_version,
                work_repository=work_repository,
                requested_as_of=requested_as_of,
                started_at=started_at,
                prompt_character_budget=prompt_character_budget,
                clock=clock,
            )
        if work_items:
            first_key = (
                work_items[0].document.asset.source_item_id,
                work_items[0].content_version,
            )
            work_items = tuple(
                item for item in work_items if (item.document.asset.source_item_id, item.content_version) == first_key
            )
        outcomes: list[InterpretationOutcome] = []
        source_item_ids: list[str] = []
        try:
            outcomes.extend(
                service.interpret_bundle_pending(
                    work_items,
                    run_id=run_id,
                    requested_as_of=requested_as_of,
                    context_known_at=model_context_at,
                    baseline_only=True,
                )
            )
            source_item_ids.extend(item.document.asset.source_item_id for item in work_items)
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


def _run_incremental_interpretation(
    state: PipelineState,
    *,
    work_items: tuple[EvidenceInterpretationWork, ...],
    evidence: EvidenceWorkRepository,
    admission: IntelligenceAdmissionRepository,
    agent: InterpretationAgent,
    implementation_version: str,
    work_repository: IntelligenceWorkRepository,
    requested_as_of: datetime,
    started_at: datetime,
    prompt_character_budget: int,
    clock: Callable[[], datetime],
) -> PipelineState:
    """Advance one durable prompt chunk and admit only a complete bundle."""
    run_id = require_run_id(state)
    claimed_at = clock()
    claimed = work_repository.claim_interpretation_chunk(
        run_id=run_id,
        claimed_at=claimed_at,
        reclaim_before=claimed_at - timedelta(minutes=30),
        source_id=state.get("source_id"),
    )
    if claimed is None and work_items:
        first_key = (work_items[0].document.asset.source_item_id, work_items[0].content_version)
        pending = evidence.list_pending_documents(
            as_of=requested_as_of,
            source_id=state.get("source_id"),
            limit=2_147_483_647,
            interpreter_version=implementation_version,
        )
        bundled = tuple(
            item for item in pending if (item.document.asset.source_item_id, item.content_version) == first_key
        )
        source_item_id, content_version = first_key
        fragments = tuple(fragment for item in bundled for fragment in item.document.fragments)
        bundle_boundary = max(item.document.asset.retrieved_at for item in bundled)
        fingerprint = ordered_work_fingerprint(tuple(fragment.fragment_id for fragment in fragments))
        bundle_id = interpretation_bundle_id(
            source_item_id=source_item_id,
            content_version=content_version,
            interpreter_version=implementation_version,
            input_fingerprint=fingerprint,
        )
        projection = project_evidence(fragments)

        def request_for(chunk: EvidenceProjectionChunk) -> InterpretationRequest:
            return InterpretationRequest(
                source_item_id=source_item_id,
                evidence=tuple(
                    EvidencePromptRecord(
                        alias=item.alias,
                        kind=item.kind,
                        text=item.text,
                        core=item.alias in chunk.core_aliases,
                    )
                    for item in chunk.evidence
                ),
                requested_as_of=bundle_boundary,
                context_known_at=bundle_boundary,
            )

        projected_chunks = tuple(
            projection.chunks_fitting(
                lambda chunk: inference_request_fits(agent, request_for(chunk), fallback=prompt_character_budget)
            )
        )
        chunk_specs = tuple(
            InterpretationChunkSpec(
                chunk_id=stable_identifier("interpretation-chunk", {"bundle_id": bundle_id, "number": number}),
                chunk_number=number,
                input_fingerprint=_work_fingerprint(tuple(item.alias for item in chunk.evidence)),
                payload=_JSON_VALUE_ADAPTER.validate_python(
                    {
                        "source_item_id": source_item_id,
                        "content_version": content_version,
                        "request": request_for(chunk).model_dump(mode="json"),
                        "core_aliases": sorted(chunk.core_aliases),
                        "alias_map": {item.alias: list(item.fragment_ids) for item in chunk.evidence},
                    }
                ),
            )
            for number, chunk in enumerate(projected_chunks)
        )
        work_repository.ensure_interpretation_bundle(
            InterpretationBundleRecord(
                bundle_id=bundle_id,
                source_item_id=source_item_id,
                content_version=content_version,
                interpreter_version=implementation_version,
                input_fingerprint=fingerprint,
                created_at=bundle_boundary,
                payload={"asset_ids": [item.document.asset.asset_id for item in bundled]},
            ),
            chunk_specs,
        )
        claimed_at = clock()
        claimed = work_repository.claim_interpretation_chunk(
            run_id=run_id,
            claimed_at=claimed_at,
            reclaim_before=claimed_at - timedelta(minutes=30),
            source_id=state.get("source_id"),
        )
    if claimed is not None:
        chunk_payload = _json_object(claimed.payload, "interpretation chunk input")
        if chunk_payload.get("legacy_missing_interpretation") is True:
            source_item_id = _string(chunk_payload["source_item_id"])
            content_version = _string(chunk_payload["content_version"])
            asset_ids = frozenset(_string_list(chunk_payload["asset_ids"]))
            legacy_work = tuple(
                item
                for item in evidence.documents_for_bundle(
                    source_item_id=source_item_id,
                    content_version=content_version,
                )
                if item.document.asset.asset_id in asset_ids
            )
            legacy_fragments = tuple(fragment for item in legacy_work for fragment in item.document.fragments)
            legacy_projection = project_evidence(legacy_fragments)
            legacy_boundary = max(item.document.asset.retrieved_at for item in legacy_work)
            chunk_payload = _json_object(
                _JSON_VALUE_ADAPTER.validate_python(
                    {
                        "source_item_id": source_item_id,
                        "content_version": content_version,
                        "request": InterpretationRequest(
                            source_item_id=source_item_id,
                            evidence=tuple(
                                EvidencePromptRecord(
                                    alias=item.alias,
                                    kind=item.kind,
                                    text=item.text,
                                    core=True,
                                )
                                for item in legacy_projection.evidence
                            ),
                            requested_as_of=legacy_boundary,
                            context_known_at=legacy_boundary,
                        ).model_dump(mode="json"),
                        "core_aliases": sorted(item.alias for item in legacy_projection.evidence),
                        "alias_map": {item.alias: list(item.fragment_ids) for item in legacy_projection.evidence},
                    }
                ),
                "legacy interpretation chunk input",
            )
        request = InterpretationRequest.model_validate(chunk_payload["request"])
        response = agent(
            request,
            context=InferenceInvocationContext(run_id=run_id, work_unit_id=claimed.chunk_id),
        ).output
        alias_map = _string_tuple_map(chunk_payload["alias_map"])
        projection = EvidenceAliasProjection(
            evidence=tuple(
                AliasedEvidence(
                    alias=record.alias,
                    kind=record.kind,
                    text=record.text,
                    fragment_ids=alias_map[record.alias],
                )
                for record in request.evidence
            )
        )
        core_aliases = frozenset(_string_list(chunk_payload["core_aliases"]))
        completed_at = clock()
        observations = tuple(
            materialize_observation(
                draft,
                source_item_id=request.source_item_id,
                projection=projection,
                known_at=completed_at,
                assertion_boundary=completed_at,
                economic_boundary=requested_as_of,
            )
            for draft in response.observations
            if _earliest_alias_is_core(draft, projection, core_aliases)
        )
        work_repository.complete_interpretation_chunk(
            chunk_id=claimed.chunk_id,
            run_id=run_id,
            completed_at=completed_at,
            output={
                "request": request.model_dump(mode="json"),
                "response": response.model_dump(mode="json"),
                "observations": [item.model_dump(mode="json") for item in observations],
                "alias_map": {key: list(value) for key, value in alias_map.items()},
            },
        )
    ready_bundle_id = work_repository.ready_interpretation_bundle(state.get("source_id"))
    if ready_bundle_id is None:
        return {"completed_stages": completed_with(state, Stage.A1.value), "decision_at": clock()}
    chunks = work_repository.interpretation_chunks_for_bundle(ready_bundle_id)
    first_payload = next(
        (
            payload
            for chunk in chunks
            if "source_item_id" in (payload := _json_object(chunk.payload, "interpretation chunk input"))
            and "content_version" in payload
        ),
        None,
    )
    if first_payload is None:
        raise ValueError("Interpretation bundle has no source-item metadata")
    source_item_id = _string(first_payload["source_item_id"])
    content_version = _string(first_payload["content_version"])
    bundle_work = evidence.documents_for_bundle(source_item_id=source_item_id, content_version=content_version)
    outputs = tuple(
        value
        for chunk in chunks
        if isinstance(chunk.output, dict)
        and "request" in (value := _json_object(chunk.output, "interpretation chunk output"))
    )
    requests = tuple(InterpretationRequest.model_validate(item["request"]) for item in outputs)
    responses = tuple(InterpretationDraft.model_validate(item["response"]) for item in outputs)
    chunk_observations = tuple(
        ClaimObservation.model_validate(observation)
        for item in outputs
        for observation in _json_list(item["observations"])
    )
    reusable_by_asset = {
        item.document.asset.asset_id: reusable
        for item in bundle_work
        if (
            reusable := evidence.reusable_interpretation(
                item,
                interpreter_version=implementation_version,
            )
        )
        is not None
    }
    preserved_legacy_by_asset = {
        item.document.asset.asset_id: preserved
        for item in bundle_work
        if item.document.asset.asset_id not in reusable_by_asset
        and (preserved := evidence.preserved_legacy_interpretation(item)) is not None
    }
    completed_by_asset = {**preserved_legacy_by_asset, **reusable_by_asset}
    observations = tuple(
        {
            item.observation_id: item
            for item in (
                *chunk_observations,
                *(observation for reusable in completed_by_asset.values() for observation in reusable.observations),
            )
        }.values()
    )
    new_work = tuple(item for item in bundle_work if item.document.asset.asset_id not in completed_by_asset)
    new_attempt_ids = tuple(
        evidence.begin_interpretation(
            item,
            run_id=run_id,
            interpreter_version=implementation_version,
            started_at=clock(),
        )
        for item in new_work
    )
    attempt_ids = tuple(
        (
            completed_by_asset[item.document.asset.asset_id].attempt_id
            if item.document.asset.asset_id in completed_by_asset
            else new_attempt_ids[new_work.index(item)]
        )
        for item in bundle_work
    )
    known_at = clock()
    payload = InterpretationArtifactPayload(
        source_item_ids=(source_item_id,),
        observation_ids=tuple(item.observation_id for item in observations),
        attempt_ids=attempt_ids,
        fragment_ids=tuple(fragment.fragment_id for item in bundle_work for fragment in item.document.fragments),
        requests=requests,
        responses=responses,
        alias_maps=tuple(_string_tuple_map(item["alias_map"]) for item in outputs),
    )
    artifact = build_stage_artifact(
        run_id=run_id,
        stage=Stage.A1,
        requested_as_of=requested_as_of,
        started_at=started_at,
        known_at=known_at,
        decision_at=known_at,
        input_ids=tuple(
            dict.fromkeys(
                (
                    *(
                        binding
                        for work in bundle_work
                        for binding in (
                            bind_artifact_record(
                                ArtifactRecordKind.SOURCE_ITEM_VERSION,
                                f"{work.document.asset.source_item_id}@{work.content_version}",
                            ),
                            bind_artifact_record(ArtifactRecordKind.ASSET, work.document.asset.asset_id),
                            bind_artifact_record(ArtifactRecordKind.DOCUMENT, work.document.asset.asset_id),
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
            *(bind_artifact_record(ArtifactRecordKind.INTERPRETATION_ATTEMPT, value) for value in new_attempt_ids),
            *dict.fromkeys(
                bind_artifact_record(ArtifactRecordKind.OBSERVATION, observation.observation_id)
                for item in new_work
                for observation in observations
                if set(observation.evidence_fragment_ids).intersection(
                    fragment.fragment_id for fragment in item.document.fragments
                )
            ),
        ),
        implementation_version=implementation_version,
        payload=payload,
    )
    admission.admit_interpretation(
        tuple(
            InterpretationAdmission(
                attempt_id=attempt_id,
                observations=tuple(
                    observation
                    for observation in observations
                    if set(observation.evidence_fragment_ids).intersection(
                        fragment.fragment_id for fragment in item.document.fragments
                    )
                ),
            )
            for item, attempt_id in zip(new_work, new_attempt_ids, strict=True)
        ),
        completed_at=known_at,
        known_at=known_at,
        artifact=artifact,
        bundle_id=ready_bundle_id,
    )
    unit_id = stable_identifier("discovery-unit", {"bundle_id": ready_bundle_id})
    work_repository.append_discovery_unit(
        DiscoveryUnitRecord(
            unit_id=unit_id,
            kind=DiscoveryUnitKind.SOURCE_BUNDLE,
            subject_id=ready_bundle_id,
            input_fingerprint=_work_fingerprint(payload.fragment_ids),
            source_id=work_repository.source_id_for_bundle(ready_bundle_id),
            created_at=known_at,
            payload={"observation_ids": list(payload.observation_ids)},
        ),
        (DiscoveryOriginRecord(kind="interpretation_bundle", identifier=ready_bundle_id),),
    )
    _ = try_install_stage_artifact_file(require_run_dir(state), artifact)
    return {
        "completed_stages": completed_with(state, Stage.A1.value),
        "artifact_ids": (*state.get("artifact_ids", ()), artifact.artifact_id),
        "observation_ids": (*state.get("observation_ids", ()), *payload.observation_ids),
        "evidence_fragment_ids": (*state.get("evidence_fragment_ids", ()), *payload.fragment_ids),
        "interpretation_attempt_ids": (*state.get("interpretation_attempt_ids", ()), *attempt_ids),
        "decision_at": known_at,
    }


def _json_object(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _json_list(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError("Durable interpretation value must be a list")
    return value


def _string(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Durable interpretation identity must be a string")
    return value


def _string_list(value: JsonValue) -> tuple[str, ...]:
    values = _json_list(value)
    if not all(isinstance(item, str) for item in values):
        raise ValueError("Durable interpretation list must contain strings")
    return tuple(item for item in values if isinstance(item, str))


def _string_tuple_map(value: JsonValue) -> dict[str, tuple[str, ...]]:
    mapping = _json_object(value, "durable alias map")
    return {key: _string_list(item) for key, item in mapping.items()}


def _work_fingerprint(identifiers: tuple[str, ...]) -> str:
    """Return a stable fingerprint for an ordered work input."""
    return ordered_work_fingerprint(identifiers)


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
