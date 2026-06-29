"""N=1 (single-source) contract for tier_max: no corroborations → identity."""
from money_pit.compute.aggregation import tier_max
from money_pit.schemas.enums import ClaimCategory, SignalTier, SourceType
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signals import Claim

_SOURCE_REF = SourceRef(
    source_id="test-src",
    source_type=SourceType.MANUAL_NOTE,
    title="Test Source",
    url=None,
    published_at=None,
    retrieved_at="2026-01-01T00:00:00Z",
    locator=None,
)


def _claim(claim_id: str, tier: SignalTier) -> Claim:
    return Claim(
        claim_id=claim_id,
        tier=tier,
        claim="Test claim.",
        category=ClaimCategory.FUNDAMENTAL,
        tickers_affected=[],
        requires_validation=False,
        source_ref=_SOURCE_REF,
        cited_sources=[],
    )


def test_tier_max_with_empty_corroborations_preserves_claim_ids() -> None:
    claims = [_claim("c1", SignalTier.HIGH), _claim("c2", SignalTier.LOW)]
    result = tier_max(claims, [])
    assert [c.claim_id for c in result] == ["c1", "c2"]


def test_tier_max_with_empty_corroborations_preserves_tiers() -> None:
    claims = [_claim("c1", SignalTier.HIGH), _claim("c2", SignalTier.LOW)]
    result = tier_max(claims, [])
    tiers = {c.claim_id: c.tier for c in result}
    assert tiers["c1"] == SignalTier.HIGH
    assert tiers["c2"] == SignalTier.LOW


def test_tier_max_with_empty_corroborations_preserves_length() -> None:
    claims = [_claim("c1", SignalTier.HIGH), _claim("c2", SignalTier.LOW), _claim("c3", SignalTier.MEDIUM)]
    result = tier_max(claims, [])
    assert len(result) == len(claims)
