from datetime import UTC
from datetime import datetime

import pytest

from money_pit.agents.budget import serialized_inference_request_size
from money_pit.contracts import SynthesisRequest
from money_pit.pipeline.candidate_grounding import candidate_grounding_observation_ids
from money_pit.pipeline.synthesis import PromptProjectionError
from money_pit.pipeline.synthesis import _bound_synthesis_request  # pyright: ignore[reportPrivateUsage]
from money_pit.pipeline.synthesis import _project_synthesis_observation  # pyright: ignore[reportPrivateUsage]
from money_pit.pipeline.synthesis import _require_bounded_unit_context  # pyright: ignore[reportPrivateUsage]
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.research import ResearchEvidenceRecord
from money_pit.schemas.sources import TrustLevel
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.schemas.universe import UniverseLayer


_NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


def _observation(
    observation_id: str,
    *,
    instruments: tuple[str, ...],
    evidence_fragment_count: int = 1,
) -> ClaimObservation:
    return ClaimObservation(
        observation_id=observation_id,
        claim_text=f"Material statement about {', '.join(instruments)}",
        claim_kind=ClaimKind.FACTUAL,
        category=ClaimCategory.FUNDAMENTAL,
        source_item_id="source-item",
        evidence_fragment_ids=tuple(f"fragment:{index:04d}" for index in range(evidence_fragment_count)),
        asserted_at=_NOW,
        known_at=_NOW,
        horizon_class=HorizonClass.TACTICAL,
        instruments=instruments,
    )


def test__candidate_grounding_observation_ids_selects_exact_structured_reference() -> None:
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-1",
        subject="Data-center power demand",
        direction=ThesisDirection.LONG,
        instrument_reference=None,
        horizon_class=HorizonClass.STRUCTURAL,
        discovery_basis=DiscoveryBasis(
            universe_layer=UniverseLayer.SOURCE_MENTION,
            universe_reference="DATA CENTERS",
        ),
        created_at=_NOW,
        known_at=_NOW,
    )
    relevant = _observation("relevant", instruments=("DATA CENTERS",))
    unrelated = _observation("unrelated", instruments=("BONDS",))

    grounded = candidate_grounding_observation_ids(
        candidate,
        observations=(unrelated, relevant),
        claims=(),
    )

    assert grounded == (relevant.observation_id,)


def test__candidate_grounding_observation_ids_selects_only_active_claim_members() -> None:
    claim_key = "claim-key"
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-claim",
        subject="Claim-grounded candidate",
        direction=ThesisDirection.LONG,
        instrument_reference="NEW",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=(claim_key,)),
        created_at=_NOW,
        known_at=_NOW,
    )
    active = _observation("active", instruments=("NEW",))
    inactive = _observation("inactive", instruments=("NEW",))
    claim = CanonicalClaim(
        canonical_claim_key=claim_key,
        current_status=ClaimStatus.ACTIVE,
        active_observation_ids=(active.observation_id,),
        projected_as_of=_NOW,
        last_material_change_at=_NOW,
        freshness_policy_version="test-policy",
    )

    grounded = candidate_grounding_observation_ids(
        candidate,
        observations=(inactive, active),
        claims=(claim,),
    )

    assert grounded == (active.observation_id,)


def test__project_synthesis_observation_excludes_fragment_provenance() -> None:
    observation = _observation(
        "large-provenance",
        instruments=("DATA CENTERS",),
        evidence_fragment_count=500,
    )

    projected = _project_synthesis_observation(observation)

    assert "evidence_fragment_ids" not in projected.model_dump()
    assert len(projected.model_dump_json()) < len(observation.model_dump_json()) / 10


def test__require_bounded_unit_context_accepts_research_anchor_without_canonical_claim() -> None:
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-universe",
        subject="Data-center power and infrastructure demand",
        direction=ThesisDirection.LONG,
        instrument_reference=None,
        horizon_class=HorizonClass.STRUCTURAL,
        discovery_basis=DiscoveryBasis(
            universe_layer=UniverseLayer.SOURCE_MENTION,
            universe_reference="DATA CENTERS",
        ),
        created_at=_NOW,
        known_at=_NOW,
    )
    research_context = ResearchCumulativeContext(
        material_anchor_assessment=MaterialAnchorAssessment(
            material_claim_keys=("data_center_electricity_demand_growth",),
            unresolved_claim_keys=("data_center_electricity_demand_growth",),
        ),
    )
    request = SynthesisRequest(
        candidates=(candidate,),
        claims=(),
        observations=(),
        resolution_candidate_sets=(),
        prior_revisions=(),
        requested_as_of=_NOW,
        context_known_at=_NOW,
        research_context=research_context,
    )

    bounded = _bound_synthesis_request(
        request,
        serialized_inference_request_size(request),
    )

    _require_bounded_unit_context(
        bounded,
        {"candidate_id": candidate.candidate_thesis_id},
    )


def test__require_bounded_unit_context_rejects_dropped_candidate_claim() -> None:
    claim_key = "canonical-source-claim"
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-claim",
        subject="Claim-grounded candidate",
        direction=ThesisDirection.LONG,
        instrument_reference="ISSUER",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=(claim_key,)),
        created_at=_NOW,
        known_at=_NOW,
    )
    request = SynthesisRequest(
        candidates=(candidate,),
        claims=(),
        observations=(),
        resolution_candidate_sets=(),
        prior_revisions=(),
        requested_as_of=_NOW,
        context_known_at=_NOW,
        research_context=ResearchCumulativeContext(
            material_anchor_assessment=MaterialAnchorAssessment(),
        ),
    )

    with pytest.raises(PromptProjectionError):
        _require_bounded_unit_context(
            request,
            {"candidate_id": candidate.candidate_thesis_id},
        )


def test__bound_synthesis_request_rejects_budget_that_would_drop_required_semantics() -> None:
    claim_key = "material-claim"
    request = SynthesisRequest(
        candidates=(
            CandidateThesis(
                candidate_thesis_id="candidate-1",
                subject="Required candidate " + "c" * 200,
                direction=ThesisDirection.LONG,
                instrument_reference="ISSUER",
                horizon_class=HorizonClass.TACTICAL,
                discovery_basis=DiscoveryBasis(source_claim_keys=(claim_key,)),
                created_at=_NOW,
                known_at=_NOW,
            ),
        ),
        claims=(
            CanonicalClaim(
                canonical_claim_key=claim_key,
                current_status=ClaimStatus.ACTIVE,
                active_observation_ids=("observation-1",),
                projected_as_of=_NOW,
                last_material_change_at=_NOW,
                freshness_policy_version="test-policy",
            ),
        ),
        observations=(),
        resolution_candidate_sets=(),
        prior_revisions=(),
        requested_as_of=_NOW,
        context_known_at=_NOW,
        research_context=ResearchCumulativeContext(
            evidence=(
                ResearchEvidenceRecord(
                    alias="E000001",
                    kind="filing",
                    text="Required primary evidence " + "e" * 200,
                    provenance_group="issuer-primary",
                    trust_level=TrustLevel.AUTHORITATIVE_PRIMARY,
                ),
            ),
            material_anchor_assessment=MaterialAnchorAssessment(
                material_claim_keys=(claim_key,),
                supported_claim_keys=(claim_key,),
                evidence_standard_satisfied=True,
            ),
        ),
    )

    with pytest.raises(PromptProjectionError):
        _ = _bound_synthesis_request(
            request,
            serialized_inference_request_size(request) - 1,
        )
