from datetime import UTC
from datetime import datetime
from datetime import timedelta

from money_pit.claims.projection import ClaimFreshnessRule
from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.projection import project_canonical_claim
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus


_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _policy() -> ClaimRefreshPolicy:
    rule = ClaimFreshnessRule(
        review_interval=timedelta(days=1),
        freshness_interval=timedelta(days=7),
    )
    return ClaimRefreshPolicy(
        policy_version="freshness-1",
        rules={category: dict.fromkeys(HorizonClass, rule) for category in ClaimCategory},
    )


def _observation(*, valid_until: datetime | None = None) -> ClaimObservation:
    return ClaimObservation(
        observation_id="observation-1",
        claim_text="Revenue grew.",
        claim_kind=ClaimKind.FACTUAL,
        category=ClaimCategory.FUNDAMENTAL,
        source_item_id="source:item",
        evidence_fragment_ids=("fragment-1",),
        asserted_at=_NOW,
        known_at=_NOW,
        effective_from=_NOW,
        review_at=_NOW + timedelta(days=7),
        valid_until=valid_until,
        horizon_class=HorizonClass.TACTICAL,
        instruments=("NEW",),
    )


def test_project_canonical_claim_marks_a_current_contradiction_disputed() -> None:
    verification = VerificationResult(
        verification_id="verification-1",
        observation_id="observation-1",
        status=VerificationStatus.CONTRADICTED,
        checked_at=_NOW,
        known_at=_NOW,
        verifier_version="verifier-v1",
    )

    projection = project_canonical_claim(
        (_observation(),),
        (verification,),
        canonical_claim_key="claim-1",
        as_of=_NOW,
        refresh_policy=_policy(),
    )

    assert projection.current_status is ClaimStatus.DISPUTED


def test_project_canonical_claim_expires_at_the_economic_boundary() -> None:
    projection = project_canonical_claim(
        (_observation(valid_until=_NOW),),
        (),
        canonical_claim_key="claim-1",
        as_of=_NOW,
        refresh_policy=_policy(),
    )

    assert projection.current_status is ClaimStatus.EXPIRED


def test_project_canonical_claim_excludes_late_known_evidence() -> None:
    late = _observation().model_copy(update={"known_at": _NOW + timedelta(days=1)})
    early = _observation()

    projection = project_canonical_claim(
        (early, late),
        (),
        canonical_claim_key="claim-1",
        as_of=_NOW,
        refresh_policy=_policy(),
    )

    assert projection.active_observation_ids == (early.observation_id,)


def test_project_canonical_claim_uses_latest_verification_correction() -> None:
    contradicted = VerificationResult(
        verification_id="verification-1",
        observation_id="observation-1",
        status=VerificationStatus.CONTRADICTED,
        checked_at=_NOW,
        known_at=_NOW,
        verifier_version="verifier-v1",
    )
    corrected_at = _NOW + timedelta(hours=1)
    supported = VerificationResult(
        verification_id="verification-2",
        observation_id="observation-1",
        status=VerificationStatus.SUPPORTED,
        checked_at=corrected_at,
        known_at=corrected_at,
        valid_until=corrected_at + timedelta(days=2),
        verifier_version="verifier-v2",
    )

    projection = project_canonical_claim(
        (_observation(),),
        (contradicted, supported),
        canonical_claim_key="claim-1",
        as_of=corrected_at,
        refresh_policy=_policy(),
        resolution_change_times=(corrected_at - timedelta(minutes=1),),
    )

    assert projection.current_status is ClaimStatus.ACTIVE
    assert projection.last_material_change_at == corrected_at
    assert projection.next_refresh_at == corrected_at + timedelta(days=1)
