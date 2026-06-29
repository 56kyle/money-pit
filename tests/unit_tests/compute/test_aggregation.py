"""Tests for money_pit.compute.aggregation."""
from money_pit.compute.aggregation import compute_run_actionable, tier_max, union_claims
from money_pit.schemas.enums import ClaimCategory, ClaimRelationType, SignalTier, SourceType
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signals import Claim, CorroborationEntry, SignalSet

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


def _signal_set(slug: str, claims: list[Claim], has_actionable: bool) -> SignalSet:
    return SignalSet(
        slug=slug,
        source_ref=_SOURCE_REF,
        summary="Test summary.",
        claims=claims,
        tickers_mentioned=[],
        sectors_mentioned=[],
        macro_themes=[],
        has_actionable_content=has_actionable,
    )


def test_union_claims_with_single_signal_set() -> None:
    c1 = _claim("c1", SignalTier.HIGH)
    c2 = _claim("c2", SignalTier.LOW)
    ss = _signal_set("s1", [c1, c2], has_actionable=True)
    result = union_claims([ss])
    assert {c.claim_id for c in result} == {"c1", "c2"}


def test_union_claims_with_no_overlapping_ids() -> None:
    ss1 = _signal_set("s1", [_claim("c1", SignalTier.HIGH)], has_actionable=True)
    ss2 = _signal_set("s2", [_claim("c2", SignalTier.LOW)], has_actionable=False)
    result = union_claims([ss1, ss2])
    assert {c.claim_id for c in result} == {"c1", "c2"}
    assert len(result) == 2


def test_union_claims_with_duplicate_claim_id() -> None:
    shared = _claim("c1", SignalTier.HIGH)
    ss1 = _signal_set("s1", [shared], has_actionable=True)
    ss2 = _signal_set("s2", [shared, _claim("c2", SignalTier.LOW)], has_actionable=False)
    result = union_claims([ss1, ss2])
    ids = [c.claim_id for c in result]
    assert ids.count("c1") == 1
    assert "c2" in ids


def test_tier_max_with_empty_corroborations() -> None:
    claims = [_claim("c1", SignalTier.LOW), _claim("c2", SignalTier.MEDIUM)]
    result = tier_max(claims, [])
    assert {c.claim_id for c in result} == {"c1", "c2"}
    tiers = {c.claim_id: c.tier for c in result}
    assert tiers["c1"] == SignalTier.LOW
    assert tiers["c2"] == SignalTier.MEDIUM


def test_compute_run_actionable_with_all_false() -> None:
    sets = [
        _signal_set("s1", [], has_actionable=False),
        _signal_set("s2", [], has_actionable=False),
    ]
    assert compute_run_actionable(sets) is False


def test_compute_run_actionable_with_any_true() -> None:
    sets = [
        _signal_set("s1", [], has_actionable=False),
        _signal_set("s2", [], has_actionable=True),
    ]
    assert compute_run_actionable(sets) is True


def test_tier_max_upgrades_lower_tier_in_group() -> None:
    claims = [_claim("c1", SignalTier.LOW), _claim("c2", SignalTier.HIGH)]
    corroboration = CorroborationEntry(relation=ClaimRelationType.AGREE, claim_ids=["c1", "c2"])
    result = tier_max(claims, [corroboration])
    tiers = {c.claim_id: c.tier for c in result}
    assert tiers["c1"] == SignalTier.HIGH
    assert tiers["c2"] == SignalTier.HIGH
