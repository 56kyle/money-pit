"""Claim union, tier max across corroborations, run-level actionability flag."""
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.signals import Claim, CorroborationEntry, SignalSet

_TIER_ORDER: dict[SignalTier, int] = {
    SignalTier.HIGH: 3,
    SignalTier.MEDIUM: 2,
    SignalTier.LOW: 1,
    SignalTier.PORTFOLIO: 0,
}


def union_claims(signal_sets: list[SignalSet]) -> list[Claim]:
    seen: set[str] = set()
    result: list[Claim] = []
    for signal_set in signal_sets:
        for claim in signal_set.claims:
            if claim.claim_id not in seen:
                seen.add(claim.claim_id)
                result.append(claim)
    return result


def tier_max(claims: list[Claim], corroborations: list[CorroborationEntry]) -> list[Claim]:
    if not corroborations:
        return list(claims)
    id_to_claim: dict[str, Claim] = {c.claim_id: c for c in claims}
    for entry in corroborations:
        group_ids = entry.claim_ids
        max_tier = max(
            (id_to_claim[cid].tier for cid in group_ids if cid in id_to_claim),
            key=lambda t: _TIER_ORDER[t],
            default=None,
        )
        if max_tier is None:
            continue
        for cid in group_ids:
            if cid in id_to_claim:
                claim = id_to_claim[cid]
                if _TIER_ORDER[claim.tier] < _TIER_ORDER[max_tier]:
                    id_to_claim[cid] = claim.model_copy(update={"tier": max_tier})
    return [id_to_claim[c.claim_id] for c in claims]


def compute_run_actionable(signal_sets: list[SignalSet]) -> bool:
    return any(s.has_actionable_content for s in signal_sets)
