from datetime import UTC
from datetime import datetime

import pytest

from money_pit.agents.budget import serialized_inference_request_size
from money_pit.contracts import SynthesisRequest
from money_pit.pipeline.synthesis import PromptProjectionError
from money_pit.pipeline.synthesis import _bound_synthesis_request  # pyright: ignore[reportPrivateUsage]
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.research import ResearchEvidenceRecord
from money_pit.schemas.sources import TrustLevel
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis


_NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


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
