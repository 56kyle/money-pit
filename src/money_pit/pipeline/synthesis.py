"""Module implementing A4 resolution, verification, and temporal thesis synthesis."""

from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.agents.budget import request_character_allowance
from money_pit.agents.budget import serialized_inference_request_size
from money_pit.contracts import ClaimMemory
from money_pit.contracts import ContributionDraft
from money_pit.contracts import ResolutionCandidateSet
from money_pit.contracts import ResolutionDraft
from money_pit.contracts import SynthesisAgent
from money_pit.contracts import SynthesisDraft
from money_pit.contracts import SynthesisRequest
from money_pit.contracts import ThesisMemory
from money_pit.contracts import ThesisRevisionDraft
from money_pit.contracts import VerificationDraft
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
from money_pit.pipeline.identity import stable_identifier
from money_pit.pipeline.research import reindex_research_aliases
from money_pit.pipeline.temporal import TemporalCompatibility
from money_pit.pipeline.temporal import require_model_temporal_authority
from money_pit.pipeline.temporal import temporal_compatibility
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimResolutionDecision
from money_pit.schemas.claims import ClaimResolutionKind
from money_pit.schemas.claims import UnresolvedObservationCursor
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.schemas.research import EvidenceAliasBinding
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import ProvisionalAnchorEvidence
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.research import ResearchEvidenceRecord
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import bind_artifact_record
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import TrustLevel
from money_pit.schemas.temporal import CausalBridgeKind
from money_pit.schemas.temporal import SignalContribution
from money_pit.schemas.temporal import SignalContributionKind
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisRevision
from money_pit.schemas.theses import ThesisStatus
from money_pit.storage.admission import IntelligenceAdmissionRepository


class UnknownSynthesisReferenceError(Exception):
    """Raised when A4 output references data outside its point-in-time input."""


class AmbiguousThesisSubjectError(Exception):
    """Raised when an A4 contribution cannot resolve one new revision."""


class VerificationAdmissibilityError(Exception):
    """Raised when agent verification claims exceed cited evidence authority."""


class ResolutionCandidateBoundaryError(Exception):
    """Raised when a binary resolution escapes its bounded candidate set."""


class PromptProjectionError(Exception):
    """Raised when one atomic A4 input cannot fit the configured prompt budget."""


class InvalidThesisLifecycleError(Exception):
    """Raised when a revision draft cannot enter a deterministic lifecycle state."""


class SynthesisArtifactPayload(BaseModel):
    """Immutable A4 audit payload."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    resolution_ids: tuple[str, ...]
    verification_ids: tuple[str, ...]
    thesis_revision_ids: tuple[str, ...]
    contribution_ids: tuple[str, ...]
    request: SynthesisRequest
    response: SynthesisDraft
    alias_bindings: tuple[EvidenceAliasBinding, ...]
    unresolved_subject_limit: int
    unresolved_page_next_cursor: UnresolvedObservationCursor | None = None


_MAX_A4_UNRESOLVED_SUBJECTS = 50


@dataclass(frozen=True)
class _RevisionTarget:
    thesis_id: str
    revision_number: int
    promoted_from_candidate_id: str | None
    prior: ThesisRevision | None


def materialize_resolution(
    draft: ResolutionDraft,
    *,
    observations: Mapping[str, ClaimObservation],
    candidate_ids_by_subject: Mapping[str, frozenset[str]],
    decision_at: datetime,
    known_at: datetime,
    resolver_version: str,
) -> ClaimResolutionDecision:
    """Validate A4 observation references and assign immutable identity."""
    if draft.subject_observation_id not in observations:
        raise UnknownSynthesisReferenceError("Resolution references an observation not visible as_of")
    relation = ClaimResolutionKind(draft.relation)
    if draft.object_observation_id is not None and draft.object_observation_id not in observations:
        raise UnknownSynthesisReferenceError("Resolution references an observation not visible as_of")
    if draft.object_observation_id is not None and draft.object_observation_id not in candidate_ids_by_subject.get(
        draft.subject_observation_id, frozenset()
    ):
        raise ResolutionCandidateBoundaryError(
            "Binary resolution object is outside the subject's bounded candidate set",
        )
    decision_id: str = stable_identifier(
        "resolution",
        {
            "subject": draft.subject_observation_id,
            "object": draft.object_observation_id,
            "relation": draft.relation,
            "decision_at": decision_at.isoformat(),
        },
    )
    return ClaimResolutionDecision(
        decision_id=decision_id,
        subject_observation_id=draft.subject_observation_id,
        object_observation_id=draft.object_observation_id,
        relation=relation,
        decided_at=decision_at,
        known_at=known_at,
        resolver_version=resolver_version,
        rationale=draft.rationale,
    )


def materialize_verification(
    draft: VerificationDraft,
    *,
    observations: Mapping[str, ClaimObservation],
    alias_bindings: tuple[EvidenceAliasBinding, ...],
    decision_at: datetime,
    known_at: datetime,
    verifier_version: str,
) -> VerificationResult:
    """Validate A4 verification references and assign immutable identity."""
    if draft.observation_id not in observations:
        raise UnknownSynthesisReferenceError("Verification references an observation not visible as_of")
    subject_observation = observations[draft.observation_id]
    binding_by_alias = {binding.alias: binding for binding in alias_bindings}
    cited_aliases = (*draft.supporting_evidence_aliases, *draft.contradicting_evidence_aliases)
    try:
        cited_bindings = tuple(binding_by_alias[alias] for alias in cited_aliases)
    except KeyError as error:
        raise UnknownSynthesisReferenceError(
            f"Verification cites an unknown evidence alias: {error.args[0]}"
        ) from error
    if any(AllowedUse.FACTUAL_VERIFICATION not in binding.allowed_uses for binding in cited_bindings):
        raise UnknownSynthesisReferenceError("Verification cites evidence not authorized for factual verification")
    supporting_bindings = tuple(binding_by_alias[alias] for alias in draft.supporting_evidence_aliases)
    contradicting_bindings = tuple(binding_by_alias[alias] for alias in draft.contradicting_evidence_aliases)
    eligible_support = tuple(
        binding
        for binding in supporting_bindings
        if binding.trust_level not in {TrustLevel.COMMENTARY, TrustLevel.UNTRUSTED}
        and binding.source_item_id != subject_observation.source_item_id
    )
    eligible_contradiction = tuple(
        binding
        for binding in contradicting_bindings
        if binding.trust_level not in {TrustLevel.COMMENTARY, TrustLevel.UNTRUSTED}
        and binding.source_item_id != subject_observation.source_item_id
    )
    derived_groups = tuple(sorted({binding.provenance_group for binding in eligible_support}))
    has_primary_support = any(binding.trust_level is TrustLevel.AUTHORITATIVE_PRIMARY for binding in eligible_support)
    independent_support_groups = {binding.provenance_group for binding in eligible_support}
    support_standard_met = has_primary_support or len(independent_support_groups) >= 2
    has_primary_contradiction = any(
        binding.trust_level is TrustLevel.AUTHORITATIVE_PRIMARY for binding in eligible_contradiction
    )
    independent_contradiction_groups = {binding.provenance_group for binding in eligible_contradiction}
    contradiction_standard_met = has_primary_contradiction or len(independent_contradiction_groups) >= 2
    status = VerificationStatus(draft.status)
    if status in {VerificationStatus.SUPPORTED, VerificationStatus.MIXED} and not support_standard_met:
        raise VerificationAdmissibilityError(
            "Supported verification requires primary authority or two provenance groups"
        )
    if status in {VerificationStatus.CONTRADICTED, VerificationStatus.MIXED} and not contradiction_standard_met:
        raise VerificationAdmissibilityError(
            "Contradicted verification requires primary authority or two provenance groups",
        )
    if status is VerificationStatus.SUPPORTED and contradicting_bindings:
        raise VerificationAdmissibilityError("Supported verification cannot omit cited contradiction from its status")
    supporting_ids = tuple(
        dict.fromkeys(
            fragment_id
            for alias in draft.supporting_evidence_aliases
            for fragment_id in binding_by_alias[alias].fragment_ids
        )
    )
    contradicting_ids = tuple(
        dict.fromkeys(
            fragment_id
            for alias in draft.contradicting_evidence_aliases
            for fragment_id in binding_by_alias[alias].fragment_ids
        )
    )
    verification_id: str = stable_identifier(
        "verification",
        {
            "observation_id": draft.observation_id,
            "status": draft.status,
            "supporting": list(supporting_ids),
            "contradicting": list(contradicting_ids),
            "decision_at": decision_at.isoformat(),
        },
    )
    return VerificationResult(
        verification_id=verification_id,
        observation_id=draft.observation_id,
        status=status,
        supporting_evidence_ids=supporting_ids,
        contradicting_evidence_ids=contradicting_ids,
        independent_provenance_groups=derived_groups,
        checked_at=decision_at,
        known_at=known_at,
        valid_until=draft.valid_until,
        verifier_version=verifier_version,
        limitations=draft.limitations,
    )


def materialize_revision(
    draft: ThesisRevisionDraft,
    *,
    candidates: tuple[CandidateThesis, ...],
    prior_revisions: tuple[ThesisRevision, ...],
    observations: Mapping[str, ClaimObservation],
    canonical_keys_by_observation: Mapping[str, str],
    verifications_by_observation: Mapping[str, VerificationResult],
    decision_at: datetime,
    known_at: datetime,
) -> ThesisRevision:
    """Assign deterministic thesis and revision identities without mutating history."""
    supporting_claim_keys, contradicting_claim_keys = _resolved_revision_evidence(
        draft,
        observations=observations,
        canonical_keys_by_observation=canonical_keys_by_observation,
    )
    target = _resolve_revision_target(draft, candidates, prior_revisions)
    status = _determine_revision_status(
        draft,
        prior=target.prior,
        verifications_by_observation=verifications_by_observation,
        decision_at=decision_at,
    )
    revision_id: str = stable_identifier(
        "thesis-revision",
        {
            "thesis_id": target.thesis_id,
            "revision_number": target.revision_number,
            "decision_at": decision_at.isoformat(),
        },
    )
    return ThesisRevision(
        revision_id=revision_id,
        thesis_id=target.thesis_id,
        revision_number=target.revision_number,
        promoted_from_candidate_id=target.promoted_from_candidate_id,
        subject=draft.subject,
        instrument=draft.instrument,
        theme=draft.theme,
        direction=draft.direction,
        status=status,
        horizon_class=draft.horizon_class,
        effective_from=draft.effective_from,
        event_at=draft.event_at,
        review_at=draft.review_at,
        valid_until=draft.valid_until,
        scenario_distribution=draft.scenario_distribution,
        invalidation_rules=draft.invalidation_rules,
        supporting_claim_keys=supporting_claim_keys,
        contradicting_claim_keys=contradicting_claim_keys,
        causal_mechanisms=draft.causal_mechanisms,
        regime_assumptions=draft.regime_assumptions,
        confidence=draft.confidence,
        reasoning=draft.reasoning,
        created_at=decision_at,
        known_at=known_at,
    )


def _resolved_revision_evidence(
    draft: ThesisRevisionDraft,
    *,
    observations: Mapping[str, ClaimObservation],
    canonical_keys_by_observation: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    observation_ids = {
        *draft.supporting_observation_ids,
        *draft.contradicting_observation_ids,
    }
    if not observation_ids <= observations.keys():
        raise UnknownSynthesisReferenceError("Thesis revision cites an observation not visible as_of")
    if set(draft.supporting_observation_ids) & set(draft.contradicting_observation_ids):
        raise InvalidThesisLifecycleError("One observation cannot both support and contradict a revision")
    try:
        supporting = tuple(
            dict.fromkeys(canonical_keys_by_observation[item] for item in draft.supporting_observation_ids),
        )
        contradicting = tuple(
            dict.fromkeys(canonical_keys_by_observation[item] for item in draft.contradicting_observation_ids),
        )
    except KeyError as error:
        raise InvalidThesisLifecycleError(
            "Thesis evidence must have an accepted canonical resolution",
        ) from error
    return supporting, contradicting


def _resolve_revision_target(
    draft: ThesisRevisionDraft,
    candidates: tuple[CandidateThesis, ...],
    prior_revisions: tuple[ThesisRevision, ...],
) -> _RevisionTarget:
    if draft.promoted_from_candidate_id is not None:
        candidate_ids = {candidate.candidate_thesis_id for candidate in candidates}
        if draft.promoted_from_candidate_id not in candidate_ids:
            raise UnknownSynthesisReferenceError("Thesis revision promotes a candidate not visible as_of")
        thesis_id = stable_identifier(
            "thesis",
            {"candidate_id": draft.promoted_from_candidate_id},
        )
        if any(revision.thesis_id == thesis_id for revision in prior_revisions):
            raise InvalidThesisLifecycleError("A candidate can be promoted only once")
        return _RevisionTarget(thesis_id, 1, draft.promoted_from_candidate_id, None)

    prior_by_id = {revision.revision_id: revision for revision in prior_revisions}
    try:
        prior = prior_by_id[draft.revises_revision_id or ""]
    except KeyError as error:
        raise UnknownSynthesisReferenceError("Revision target is not visible as_of") from error
    latest = max(
        (revision for revision in prior_revisions if revision.thesis_id == prior.thesis_id),
        key=lambda revision: revision.revision_number,
    )
    if latest.revision_id != prior.revision_id:
        raise InvalidThesisLifecycleError("Revision must extend the latest visible thesis revision")
    if prior.status in {ThesisStatus.INVALIDATED, ThesisStatus.CLOSED}:
        raise InvalidThesisLifecycleError("Terminal theses cannot be revived by a later revision")
    return _RevisionTarget(prior.thesis_id, prior.revision_number + 1, None, prior)


def _determine_revision_status(
    draft: ThesisRevisionDraft,
    *,
    prior: ThesisRevision | None,
    verifications_by_observation: Mapping[str, VerificationResult],
    decision_at: datetime,
) -> ThesisStatus:
    support_verified = bool(draft.supporting_observation_ids) and all(
        (verification := verifications_by_observation.get(observation_id)) is not None
        and verification.status is VerificationStatus.SUPPORTED
        for observation_id in draft.supporting_observation_ids
    )
    contradiction_verified = any(
        (verification := verifications_by_observation.get(observation_id)) is not None
        and verification.status in {VerificationStatus.CONTRADICTED, VerificationStatus.MIXED}
        for observation_id in draft.contradicting_observation_ids
    )
    if draft.triggered_invalidation_rules:
        if prior is None:
            raise InvalidThesisLifecycleError("A first revision cannot trigger a prior invalidation rule")
        if not set(draft.triggered_invalidation_rules) <= set(prior.invalidation_rules):
            raise InvalidThesisLifecycleError("Triggered invalidation rule is not present in the prior revision")
        if not contradiction_verified:
            raise InvalidThesisLifecycleError("Invalidation requires a verified contradicting observation")
        return ThesisStatus.INVALIDATED
    if prior is not None and prior.valid_until is not None and prior.valid_until <= decision_at:
        return ThesisStatus.CLOSED
    if contradiction_verified:
        return ThesisStatus.WEAKENED
    if support_verified:
        return ThesisStatus.ACTIVE
    return ThesisStatus.CANDIDATE if prior is None else prior.status


def materialize_contribution(
    draft: ContributionDraft,
    *,
    observations: dict[str, ClaimObservation],
    revisions_by_subject: dict[str, list[ThesisRevision]],
    decision_at: datetime,
    known_at: datetime,
    synthesis_version: str,
    accepted_update_observation_ids: frozenset[str],
    configured_bridge_relationships: frozenset[tuple[CausalBridgeKind, str, str]],
    proxy_relationships: Mapping[str, frozenset[str]] | None = None,
) -> SignalContribution:
    """Apply deterministic compatibility before persisting an agent relation."""
    try:
        observation: ClaimObservation = observations[draft.observation_id]
    except KeyError as error:
        raise UnknownSynthesisReferenceError("Contribution references an observation not visible as_of") from error
    matches: list[ThesisRevision] = revisions_by_subject.get(draft.thesis_subject.strip().casefold(), [])
    if len(matches) != 1:
        raise AmbiguousThesisSubjectError(
            f"Contribution subject must resolve to one new revision: {draft.thesis_subject!r}",
        )
    revision: ThesisRevision = matches[0]
    bridge = draft.causal_bridge
    observation_subjects = {
        *(instrument.strip().casefold() for instrument in observation.instruments),
        *(theme.strip().casefold() for theme in observation.themes),
    }
    revision_subjects = {
        revision.subject.strip().casefold(),
        *((revision.instrument.strip().casefold(),) if revision.instrument is not None else ()),
        *((revision.theme.strip().casefold(),) if revision.theme is not None else ()),
    }
    configured = {
        (kind, source.strip().casefold(), target.strip().casefold())
        for kind, source, target in configured_bridge_relationships
    }
    accepted_update = (
        observation.observation_id in accepted_update_observation_ids
        or observation.supersedes_observation_id is not None
    )
    bridge_authorized = bool(
        bridge is not None
        and bridge.source_subject.strip().casefold() in observation_subjects
        and bridge.target_subject.strip().casefold() in revision_subjects
        and (
            (bridge.kind, bridge.source_subject.strip().casefold(), bridge.target_subject.strip().casefold())
            in configured
            or (bridge.kind is CausalBridgeKind.UPDATE and accepted_update)
        )
    )
    explicit_update = bool(
        bridge_authorized and bridge is not None and bridge.kind is CausalBridgeKind.UPDATE and (accepted_update)
    )
    compatibility: TemporalCompatibility = temporal_compatibility(
        observation,
        revision,
        as_of=decision_at,
        bridge_authorized=bridge_authorized,
        explicit_update=explicit_update,
        proxy_bridge_authorized=bool(
            bridge_authorized and bridge is not None and bridge.kind is CausalBridgeKind.PROXY
        ),
        proxy_relationships=proxy_relationships,
    )
    relation: SignalContributionKind = (
        draft.relation if compatibility.compatible else SignalContributionKind.NOT_COMPARABLE
    )
    contribution_id: str = stable_identifier(
        "contribution",
        {
            "observation_id": observation.observation_id,
            "revision_id": revision.revision_id,
            "relation": relation.value,
            "decision_at": decision_at.isoformat(),
        },
    )
    return SignalContribution(
        contribution_id=contribution_id,
        observation_id=observation.observation_id,
        thesis_revision_id=revision.revision_id,
        relation=relation,
        temporal_compatible=compatibility.compatible,
        compatibility_reasons=compatibility.reasons,
        causal_bridge=draft.causal_bridge,
        judged_at=decision_at,
        known_at=known_at,
        synthesis_version=synthesis_version,
    )


def make_synthesis_node(
    *,
    claims: ClaimMemory,
    theses: ThesisMemory,
    agent: SynthesisAgent,
    resolver_version: str,
    verifier_version: str,
    synthesis_version: str,
    admission: IntelligenceAdmissionRepository,
    proxy_relationships: Mapping[str, frozenset[str]] | None = None,
    configured_bridge_relationships: frozenset[tuple[CausalBridgeKind, str, str]] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
    model_point_in_time_certified: bool = False,
    prompt_character_budget: int = 120_000,
) -> PipelineNode:
    """Return A4 with append-only intelligence authority and deterministic gates."""
    resolved_prompt_character_budget = min(
        prompt_character_budget,
        request_character_allowance(agent, fallback=prompt_character_budget),
    )

    def node(state: PipelineState) -> PipelineState:
        require_predecessor(state, Stage.A4)
        run_id: str = require_run_id(state)
        requested_as_of = require_requested_as_of(state)
        model_context_at = clock()
        started_at = require_run_started_at(state)
        require_model_temporal_authority(
            requested_as_of=requested_as_of,
            run_started_at=started_at,
            requested_as_of_explicit=state.get("requested_as_of_explicit", False),
            point_in_time_certified=model_point_in_time_certified,
        )
        all_same_run_observation_ids = state.get("observation_ids", ())
        selected_same_run_ids = all_same_run_observation_ids[:_MAX_A4_UNRESOLVED_SUBJECTS]
        same_run_observations = claims.observations_by_ids(selected_same_run_ids)
        baseline_limit = _MAX_A4_UNRESOLVED_SUBJECTS - len(same_run_observations)
        unresolved_page = (
            claims.unresolved_observation_page(
                requested_as_of=requested_as_of,
                limit=baseline_limit,
            )
            if baseline_limit > 0
            else None
        )
        baseline_observations = () if unresolved_page is None else unresolved_page.items
        subjects = tuple(
            {item.observation_id: item for item in (*same_run_observations, *baseline_observations)}.values()
        )
        resolution_candidates_by_subject = tuple(
            (
                subject,
                claims.resolution_candidates(
                    subject.observation_id,
                    requested_as_of=requested_as_of,
                    same_run_observation_ids=selected_same_run_ids,
                    limit=20,
                ),
            )
            for subject in subjects
        )
        resolution_candidate_sets = tuple(
            ResolutionCandidateSet(
                subject_observation_id=subject.observation_id,
                candidate_observation_ids=tuple(item.observation_id for item in candidates),
            )
            for subject, candidates in resolution_candidates_by_subject
        )
        resolution_candidates = tuple(
            candidate for _, candidates in resolution_candidates_by_subject for candidate in candidates
        )
        observations = tuple({item.observation_id: item for item in (*subjects, *resolution_candidates)}.values())
        projections = claims.projections_with_deltas(
            requested_as_of=requested_as_of,
            observation_ids=all_same_run_observation_ids,
            resolution_ids=state.get("claim_resolution_decision_ids", ()),
            verification_ids=state.get("verification_result_ids", ()),
        )
        baseline_candidates = theses.candidates_as_of(as_of=requested_as_of)
        same_run_candidates = theses.candidates_by_ids(state.get("candidate_thesis_ids", ()))
        candidates = tuple(
            {item.candidate_thesis_id: item for item in (*baseline_candidates, *same_run_candidates)}.values()
        )
        prior_revisions = theses.revisions_as_of(as_of=requested_as_of)
        contexts = tuple(ResearchCumulativeContext.model_validate(item) for item in state.get("research_contexts", ()))
        research_context = _merge_contexts(contexts)
        request = SynthesisRequest(
            candidates=candidates,
            claims=projections,
            observations=observations,
            resolution_candidate_sets=resolution_candidate_sets,
            prior_revisions=prior_revisions,
            requested_as_of=requested_as_of,
            context_known_at=model_context_at,
            research_context=research_context,
            omitted_input_ids=all_same_run_observation_ids[len(selected_same_run_ids) :],
        )
        request = _bound_synthesis_request(request, resolved_prompt_character_budget)
        draft = agent(request)
        decision_at = clock()
        known_at = clock()
        observations_by_id = {item.observation_id: item for item in observations}
        expected_resolution_subjects = {item.subject_observation_id for item in request.resolution_candidate_sets}
        response_resolution_subjects = tuple(item.subject_observation_id for item in draft.resolutions)
        if (
            len(response_resolution_subjects) != len(set(response_resolution_subjects))
            or set(response_resolution_subjects) != expected_resolution_subjects
        ):
            raise ResolutionCandidateBoundaryError(
                "Synthesis must emit exactly one resolution for each bounded subject",
            )
        candidate_ids_by_subject = {
            item.subject_observation_id: frozenset(item.candidate_observation_ids)
            for item in request.resolution_candidate_sets
        }
        resolutions: tuple[ClaimResolutionDecision, ...] = tuple(
            materialize_resolution(
                item,
                observations=observations_by_id,
                candidate_ids_by_subject=candidate_ids_by_subject,
                decision_at=decision_at,
                known_at=known_at,
                resolver_version=resolver_version,
            )
            for item in draft.resolutions
        )
        verifications: tuple[VerificationResult, ...] = tuple(
            materialize_verification(
                item,
                observations=observations_by_id,
                alias_bindings=research_context.alias_bindings,
                decision_at=decision_at,
                known_at=known_at,
                verifier_version=verifier_version,
            )
            for item in draft.verifications
        )
        revision_observation_ids = tuple(
            dict.fromkeys(
                observation_id
                for revision in draft.revisions
                for observation_id in (
                    *revision.supporting_observation_ids,
                    *revision.contradicting_observation_ids,
                )
            )
        )
        canonical_keys_by_observation = claims.resolved_claim_keys(
            revision_observation_ids,
            requested_as_of=requested_as_of,
            pending_resolutions=resolutions,
        )
        if len({item.observation_id for item in verifications}) != len(verifications):
            raise VerificationAdmissibilityError(
                "Synthesis may emit at most one verification per observation",
            )
        verifications_by_observation = claims.latest_verifications_for_observations(
            revision_observation_ids,
            requested_as_of=requested_as_of,
            same_run_verification_ids=state.get("verification_result_ids", ()),
        )
        verifications_by_observation.update(
            {item.observation_id: item for item in verifications},
        )
        revisions: tuple[ThesisRevision, ...] = tuple(
            materialize_revision(
                item,
                candidates=candidates,
                prior_revisions=prior_revisions,
                observations=observations_by_id,
                canonical_keys_by_observation=canonical_keys_by_observation,
                verifications_by_observation=verifications_by_observation,
                decision_at=decision_at,
                known_at=known_at,
            )
            for item in draft.revisions
        )
        if len({revision.thesis_id for revision in revisions}) != len(revisions):
            raise InvalidThesisLifecycleError("A synthesis batch may contain only one revision per thesis")
        revisions_by_subject: dict[str, list[ThesisRevision]] = {}
        for revision in revisions:
            revisions_by_subject.setdefault(revision.subject.strip().casefold(), []).append(revision)
        contributions: tuple[SignalContribution, ...] = tuple(
            materialize_contribution(
                item,
                observations=observations_by_id,
                revisions_by_subject=revisions_by_subject,
                decision_at=decision_at,
                known_at=known_at,
                synthesis_version=synthesis_version,
                accepted_update_observation_ids=frozenset(
                    decision.subject_observation_id
                    for decision in resolutions
                    if decision.relation is ClaimResolutionKind.UPDATES
                ),
                configured_bridge_relationships=configured_bridge_relationships or frozenset(),
                proxy_relationships=proxy_relationships,
            )
            for item in draft.contributions
        )
        payload = SynthesisArtifactPayload(
            resolution_ids=tuple(item.decision_id for item in resolutions),
            verification_ids=tuple(item.verification_id for item in verifications),
            thesis_revision_ids=tuple(item.revision_id for item in revisions),
            contribution_ids=tuple(item.contribution_id for item in contributions),
            request=request,
            response=draft,
            alias_bindings=research_context.alias_bindings,
            unresolved_subject_limit=_MAX_A4_UNRESOLVED_SUBJECTS,
            unresolved_page_next_cursor=(None if unresolved_page is None else unresolved_page.next_cursor),
        )
        artifact_known_at = clock()
        artifact: StageArtifact = build_stage_artifact(
            run_id=run_id,
            stage=Stage.A4,
            requested_as_of=requested_as_of,
            started_at=started_at,
            known_at=artifact_known_at,
            decision_at=decision_at,
            input_ids=tuple(
                bind_artifact_record(ArtifactRecordKind.OBSERVATION, item.observation_id) for item in observations
            ),
            output_ids=(
                *(
                    bind_artifact_record(ArtifactRecordKind.CLAIM_RESOLUTION, identifier)
                    for identifier in payload.resolution_ids
                ),
                *(
                    bind_artifact_record(ArtifactRecordKind.VERIFICATION, identifier)
                    for identifier in payload.verification_ids
                ),
                *(
                    bind_artifact_record(ArtifactRecordKind.THESIS_REVISION, identifier)
                    for identifier in payload.thesis_revision_ids
                ),
                *(
                    bind_artifact_record(ArtifactRecordKind.SIGNAL_CONTRIBUTION, identifier)
                    for identifier in payload.contribution_ids
                ),
            ),
            implementation_version=synthesis_version,
            payload=payload,
        )
        admission.admit_synthesis(
            resolutions=resolutions,
            verifications=verifications,
            revisions=revisions,
            contributions=contributions,
            artifact=artifact,
        )
        _ = try_install_stage_artifact_file(require_run_dir(state), artifact)
        return {
            "completed_stages": completed_with(state, Stage.A4.value),
            "artifact_ids": (*state.get("artifact_ids", ()), artifact.artifact_id),
            "thesis_revision_ids": payload.thesis_revision_ids,
            "claim_resolution_decision_ids": payload.resolution_ids,
            "verification_result_ids": payload.verification_ids,
            "signal_contribution_ids": payload.contribution_ids,
            "decision_at": decision_at,
        }

    return node


def _merge_contexts(contexts: tuple[ResearchCumulativeContext, ...]) -> ResearchCumulativeContext:
    """Merge bounded round contexts without inventing evidence assessments."""
    evidence, alias_bindings = reindex_research_aliases(contexts)
    material_keys = tuple(
        dict.fromkeys(key for context in contexts for key in context.material_anchor_assessment.material_claim_keys)
    )
    supported = tuple(
        dict.fromkeys(key for context in contexts for key in context.material_anchor_assessment.supported_claim_keys)
    )
    contradicted = tuple(
        dict.fromkeys(key for context in contexts for key in context.material_anchor_assessment.contradicted_claim_keys)
    )
    provisional_groups: dict[str, set[str]] = {}
    provisional_primary: dict[str, bool] = {}
    for context in contexts:
        for item in context.material_anchor_assessment.provisional_evidence:
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
    provisional = tuple(
        item.claim_key
        for item in provisional_evidence
        if (item.has_authoritative_primary or len(item.provenance_groups) >= 2)
        and item.claim_key not in supported
        and item.claim_key not in contradicted
    )
    unresolved = tuple(
        key for key in material_keys if key not in supported and key not in provisional and key not in contradicted
    )
    provenance = tuple(sorted({group for context in contexts for group in context.provenance_groups}))
    return ResearchCumulativeContext(
        new_observations=tuple(
            {item.observation_id: item for context in contexts for item in context.new_observations}.values()
        ),
        evidence=evidence,
        alias_bindings=alias_bindings,
        provenance_groups=provenance,
        failure_kinds=tuple(kind for context in contexts for kind in context.failure_kinds),
        material_anchor_assessment=MaterialAnchorAssessment(
            material_claim_keys=material_keys,
            supported_claim_keys=supported,
            provisionally_covered_claim_keys=provisional,
            provisional_evidence=provisional_evidence,
            contradicted_claim_keys=contradicted,
            unresolved_claim_keys=unresolved,
            independent_provenance_groups=provenance,
            has_authoritative_primary=any(
                context.material_anchor_assessment.has_authoritative_primary for context in contexts
            ),
            evidence_standard_satisfied=bool(material_keys) and not unresolved and not contradicted,
            decisive_contradiction=bool(contradicted),
        ),
    )


def _bound_synthesis_request(  # noqa: C901 - explicit record kinds preserve typed projection
    request: SynthesisRequest,
    character_budget: int,
) -> SynthesisRequest:
    """Project exact A4 records while keeping resolution units indivisible."""
    context = request.research_context.model_copy(update={"new_observations": (), "evidence": (), "alias_bindings": ()})
    observations_by_id = {item.observation_id: item for item in request.observations}
    resolution_sets: list[ResolutionCandidateSet] = []
    resolution_observations: dict[str, ClaimObservation] = {}
    records: tuple[
        tuple[str, str, CandidateThesis | CanonicalClaim | ClaimObservation | ThesisRevision | ResearchEvidenceRecord],
        ...,
    ] = (
        *(("candidate", item.candidate_thesis_id, item) for item in request.candidates),
        *(("claim", item.canonical_claim_key, item) for item in request.claims),
        *(("observation", item.observation_id, item) for item in request.observations),
        *(("revision", item.revision_id, item) for item in request.prior_revisions),
        *(("evidence", item.alias, item) for item in request.research_context.evidence),
    )
    resolution_set_ids = tuple(
        f"resolution-set:{item.subject_observation_id}" for item in request.resolution_candidate_sets
    )
    all_ids = tuple(
        dict.fromkeys(
            (
                *request.omitted_input_ids,
                *resolution_set_ids,
                *(identifier for _, identifier, _ in records),
            )
        )
    )
    candidates: list[CandidateThesis] = []
    claims: list[CanonicalClaim] = []
    observations: list[ClaimObservation] = []
    revisions: list[ThesisRevision] = []
    evidence: list[ResearchEvidenceRecord] = []
    selected_ids: set[str] = set()

    def projected() -> SynthesisRequest:
        aliases = {item.alias for item in evidence}
        return request.model_copy(
            update={
                "candidates": tuple(candidates),
                "claims": tuple(claims),
                "observations": (*resolution_observations.values(), *observations),
                "resolution_candidate_sets": tuple(resolution_sets),
                "prior_revisions": tuple(revisions),
                "research_context": context.model_copy(
                    update={
                        "evidence": tuple(evidence),
                        "alias_bindings": tuple(
                            binding for binding in request.research_context.alias_bindings if binding.alias in aliases
                        ),
                    },
                ),
                "omitted_input_ids": tuple(identifier for identifier in all_ids if identifier not in selected_ids),
            },
        )

    for candidate_set, set_id in zip(request.resolution_candidate_sets, resolution_set_ids, strict=True):
        unit_ids = (
            candidate_set.subject_observation_id,
            *candidate_set.candidate_observation_ids,
        )
        try:
            unit = {identifier: observations_by_id[identifier] for identifier in unit_ids}
        except KeyError as error:
            raise PromptProjectionError(
                f"Resolution candidate set references an absent observation: {error.args[0]}",
            ) from error
        previous = dict(resolution_observations)
        resolution_sets.append(candidate_set)
        resolution_observations.update(unit)
        candidate = projected()
        if serialized_inference_request_size(candidate) <= character_budget:
            selected_ids.update((set_id, *unit_ids))
            continue
        _ = resolution_sets.pop()
        resolution_observations.clear()
        resolution_observations.update(previous)
        atomic = request.model_copy(
            update={
                "candidates": (),
                "claims": (),
                "observations": tuple(unit.values()),
                "resolution_candidate_sets": (candidate_set,),
                "prior_revisions": (),
                "research_context": context,
                "omitted_input_ids": tuple(
                    identifier for identifier in all_ids if identifier not in {set_id, *unit_ids}
                ),
            },
        )
        if serialized_inference_request_size(atomic) > character_budget:
            raise PromptProjectionError(
                f"Atomic resolution candidate set exceeds budget: {candidate_set.subject_observation_id}",
            )

    for kind, identifier, record in records:
        if kind == "observation" and identifier in resolution_observations:
            continue
        target: (
            list[CandidateThesis]
            | list[CanonicalClaim]
            | list[ClaimObservation]
            | list[ThesisRevision]
            | list[ResearchEvidenceRecord]
        )
        if kind == "candidate":
            target = candidates
        elif kind == "claim":
            target = claims
        elif kind == "observation":
            target = observations
        elif kind == "revision":
            target = revisions
        else:
            target = evidence
        target.append(record)  # pyright: ignore[reportArgumentType]
        selected_ids.add(identifier)
        candidate = projected()
        if serialized_inference_request_size(candidate) > character_budget:
            _ = target.pop()
            selected_ids.remove(identifier)
            empty = projected()
            atomic_evidence = (record,) if kind == "evidence" else ()
            atomic = request.model_copy(
                update={
                    "candidates": (record,) if kind == "candidate" else (),
                    "claims": (record,) if kind == "claim" else (),
                    "observations": (record,) if kind == "observation" else (),
                    "resolution_candidate_sets": (),
                    "prior_revisions": (record,) if kind == "revision" else (),
                    "research_context": context.model_copy(update={"evidence": atomic_evidence}),
                    "omitted_input_ids": tuple(item for item in all_ids if item != identifier),
                },
            )
            if serialized_inference_request_size(atomic) > character_budget:
                raise PromptProjectionError(f"Atomic synthesis record exceeds budget: {identifier}")
            if serialized_inference_request_size(empty) > character_budget:
                raise PromptProjectionError("Synthesis fixed context exceeds the character budget")
    bounded = projected()
    if serialized_inference_request_size(bounded) > character_budget:
        raise PromptProjectionError("Synthesis fixed context exceeds the character budget")
    return bounded
