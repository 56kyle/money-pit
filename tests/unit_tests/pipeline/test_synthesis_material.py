from datetime import UTC
from datetime import datetime
from datetime import timedelta

from money_pit.pipeline.synthesis_material import SynthesisDecision
from money_pit.pipeline.synthesis_material import project_synthesis_material
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.research import EvidenceAliasBinding
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.research import ResearchEvidenceRecord
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import TrustLevel


_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _claim(*, status: ClaimStatus = ClaimStatus.ACTIVE) -> CanonicalClaim:
    return CanonicalClaim(
        canonical_claim_key="claim:gold-price",
        current_status=status,
        active_observation_ids=("observation-1",),
        projected_as_of=_NOW,
        last_material_change_at=_NOW - timedelta(days=1),
        next_refresh_at=_NOW + timedelta(days=7),
        freshness_policy_version="freshness-v1",
    )


def _context(
    *,
    source_item_id: str = "filing-1",
    evidence_text: str = "Reported margins expanded.",
    failure_kinds: tuple[str, ...] = (),
    satisfied: bool = True,
    decisive_contradiction: bool = False,
) -> ResearchCumulativeContext:
    return ResearchCumulativeContext(
        evidence=(
            ResearchEvidenceRecord(
                alias="E000001",
                kind="filing",
                text=evidence_text,
                provenance_group="issuer-primary",
                trust_level=TrustLevel.AUTHORITATIVE_PRIMARY,
            ),
        ),
        alias_bindings=(
            EvidenceAliasBinding(
                alias="E000001",
                fragment_ids=("fragment-1",),
                source_item_id=source_item_id,
                provenance_group="issuer-primary",
                allowed_uses=(AllowedUse.THESIS_GENERATION,),
                trust_level=TrustLevel.AUTHORITATIVE_PRIMARY,
            ),
        ),
        provenance_groups=("issuer-primary",),
        failure_kinds=failure_kinds,
        material_anchor_assessment=MaterialAnchorAssessment(
            material_claim_keys=("claim:gold-price",),
            supported_claim_keys=("claim:gold-price",) if satisfied else (),
            contradicted_claim_keys=("claim:gold-price",) if decisive_contradiction else (),
            unresolved_claim_keys=() if satisfied or decisive_contradiction else ("claim:gold-price",),
            independent_provenance_groups=("issuer-primary",),
            has_authoritative_primary=satisfied,
            evidence_standard_satisfied=satisfied,
            decisive_contradiction=decisive_contradiction,
        ),
    )


def _projection(
    *,
    context: ResearchCumulativeContext | None = None,
    claim: CanonicalClaim | None = None,
    prior_revision_id: str | None = "revision-1",
):
    return project_synthesis_material(
        hypothesis_id="hypothesis:gold-miners",
        grounded_observation_ids=("observation-1",),
        material_claims=(claim or _claim(),),
        research_context=context or _context(),
        prior_revision_id=prior_revision_id,
    )


def test_project_synthesis_material_with_operational_history_change_reuses_identity() -> None:
    initial = _projection()
    retry_history = _projection(
        context=_context(
            evidence_text="The same admitted fragment rendered with different planner context.",
            failure_kinds=("timeout", "unauthorized_publisher"),
        )
    )

    assert initial.material_state_id == retry_history.material_state_id


def test_project_synthesis_material_with_new_accepted_evidence_changes_identity() -> None:
    initial = _projection()
    revised = _projection(context=_context(source_item_id="filing-2"))

    assert initial.material_state_id != revised.material_state_id


def test_project_synthesis_material_with_claim_state_change_changes_identity() -> None:
    initial = _projection()
    disputed = _projection(claim=_claim(status=ClaimStatus.DISPUTED))

    assert initial.material_state_id != disputed.material_state_id


def test_project_synthesis_material_with_prior_revision_change_changes_identity() -> None:
    initial = _projection()
    revised = _projection(prior_revision_id="revision-2")

    assert initial.material_state_id != revised.material_state_id


def test_project_synthesis_material_without_evidence_standard_is_insufficient() -> None:
    projection = _projection(context=_context(satisfied=False))

    assert projection.decision is SynthesisDecision.INSUFFICIENT_EVIDENCE


def test_project_synthesis_material_with_decisive_contradiction_is_eligible() -> None:
    projection = _projection(context=_context(satisfied=False, decisive_contradiction=True))

    assert projection.decision is SynthesisDecision.ELIGIBLE
