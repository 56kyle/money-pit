"""Tests for money_pit.compute.aggregation."""

from collections.abc import Callable

from money_pit.compute.aggregation import compute_run_actionable
from money_pit.compute.aggregation import tier_max
from money_pit.compute.aggregation import union_claims
from money_pit.schemas.enums import ClaimRelationType
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signals import Claim
from money_pit.schemas.signals import CorroborationEntry
from money_pit.schemas.signals import SignalSet


def _signal_set(
    slug: str,
    claims: list[Claim],
    has_actionable: bool,
    source_ref: SourceRef,
) -> SignalSet:
    return SignalSet(
        slug=slug,
        source_ref=source_ref,
        summary="Test summary.",
        claims=claims,
        tickers_mentioned=[],
        sectors_mentioned=[],
        macro_themes=[],
        has_actionable_content=has_actionable,
    )


def test_union_claims_with_single_signal_set(
    make_claim: Callable[[str, SignalTier], Claim],
    source_ref: SourceRef,
) -> None:
    c1 = make_claim("c1", SignalTier.HIGH)
    c2 = make_claim("c2", SignalTier.LOW)
    ss = _signal_set("s1", [c1, c2], has_actionable=True, source_ref=source_ref)
    result = union_claims([ss])
    assert {c.claim_id for c in result} == {"c1", "c2"}


def test_union_claims_with_no_overlapping_ids(
    make_claim: Callable[[str, SignalTier], Claim],
    source_ref: SourceRef,
) -> None:
    ss1 = _signal_set("s1", [make_claim("c1", SignalTier.HIGH)], has_actionable=True, source_ref=source_ref)
    ss2 = _signal_set("s2", [make_claim("c2", SignalTier.LOW)], has_actionable=False, source_ref=source_ref)
    result = union_claims([ss1, ss2])
    assert {c.claim_id for c in result} == {"c1", "c2"}
    assert len(result) == 2


def test_union_claims_with_duplicate_claim_id(
    make_claim: Callable[[str, SignalTier], Claim],
    source_ref: SourceRef,
) -> None:
    shared = make_claim("c1", SignalTier.HIGH)
    ss1 = _signal_set("s1", [shared], has_actionable=True, source_ref=source_ref)
    ss2 = _signal_set("s2", [shared, make_claim("c2", SignalTier.LOW)], has_actionable=False, source_ref=source_ref)
    result = union_claims([ss1, ss2])
    ids = [c.claim_id for c in result]
    assert ids.count("c1") == 1
    assert "c2" in ids


def test_tier_max_with_empty_corroborations(
    make_claim: Callable[[str, SignalTier], Claim],
) -> None:
    claims = [make_claim("c1", SignalTier.LOW), make_claim("c2", SignalTier.MEDIUM)]
    result = tier_max(claims, [])
    assert {c.claim_id for c in result} == {"c1", "c2"}
    tiers = {c.claim_id: c.tier for c in result}
    assert tiers["c1"] == SignalTier.LOW
    assert tiers["c2"] == SignalTier.MEDIUM


def test_compute_run_actionable_with_all_false(source_ref: SourceRef) -> None:
    sets = [
        _signal_set("s1", [], has_actionable=False, source_ref=source_ref),
        _signal_set("s2", [], has_actionable=False, source_ref=source_ref),
    ]
    assert compute_run_actionable(sets) is False


def test_compute_run_actionable_with_any_true(source_ref: SourceRef) -> None:
    sets = [
        _signal_set("s1", [], has_actionable=False, source_ref=source_ref),
        _signal_set("s2", [], has_actionable=True, source_ref=source_ref),
    ]
    assert compute_run_actionable(sets) is True


def test_tier_max_upgrades_lower_tier_in_group(
    make_claim: Callable[[str, SignalTier], Claim],
) -> None:
    claims = [make_claim("c1", SignalTier.LOW), make_claim("c2", SignalTier.HIGH)]
    corroboration = CorroborationEntry(relation=ClaimRelationType.AGREE, claim_ids=["c1", "c2"])
    result = tier_max(claims, [corroboration])
    tiers = {c.claim_id: c.tier for c in result}
    assert tiers["c1"] == SignalTier.HIGH
    assert tiers["c2"] == SignalTier.HIGH
