"""N=1 (single-source) contract for tier_max: no corroborations → identity."""
from collections.abc import Callable

from money_pit.compute.aggregation import tier_max
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.signals import Claim


def test_tier_max_with_empty_corroborations_preserves_claim_ids(
    make_claim: Callable[[str, SignalTier], Claim],
) -> None:
    claims = [make_claim("c1", SignalTier.HIGH), make_claim("c2", SignalTier.LOW)]
    result = tier_max(claims, [])
    assert [c.claim_id for c in result] == ["c1", "c2"]


def test_tier_max_with_empty_corroborations_preserves_tiers(
    make_claim: Callable[[str, SignalTier], Claim],
) -> None:
    claims = [make_claim("c1", SignalTier.HIGH), make_claim("c2", SignalTier.LOW)]
    result = tier_max(claims, [])
    tiers = {c.claim_id: c.tier for c in result}
    assert tiers["c1"] == SignalTier.HIGH
    assert tiers["c2"] == SignalTier.LOW


def test_tier_max_with_empty_corroborations_preserves_length(
    make_claim: Callable[[str, SignalTier], Claim],
) -> None:
    claims = [
        make_claim("c1", SignalTier.HIGH),
        make_claim("c2", SignalTier.LOW),
        make_claim("c3", SignalTier.MEDIUM),
    ]
    result = tier_max(claims, [])
    assert len(result) == len(claims)
