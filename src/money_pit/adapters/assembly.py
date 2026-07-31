"""Module containing the source-neutral SignalSetDraft-to-SignalSet assembly used by source adapters in the money_pit package."""

from money_pit.compute.signal_flags import has_actionable_content
from money_pit.compute.signal_flags import normalize_ticker
from money_pit.compute.signal_flags import requires_validation as is_validation_required
from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signal_draft import ClaimDraft
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.schemas.signals import Claim
from money_pit.schemas.signals import SignalSet


_NO_EVIDENCE_FRAGMENT_IDS: frozenset[str] = frozenset()


class UnknownEvidenceFragmentError(ValueError):
    """Raised when a claim cites a fragment absent from its source payload."""

    def __init__(self, claim_id: str, fragment_ids: tuple[str, ...]) -> None:
        """Record the claim and unknown fragment identifiers."""
        self.claim_id: str = claim_id
        self.fragment_ids: tuple[str, ...] = fragment_ids
        super().__init__(f"Claim {claim_id!r} cites unknown evidence fragments: {', '.join(fragment_ids)}")


def normalize_tickers(raw_tickers: list[str]) -> list[str]:
    """Normalize each raw ticker, dropping any that normalize to None."""
    return [t for t in (normalize_ticker(raw) for raw in raw_tickers) if t is not None]


def claim_from_draft(
    draft_claim: ClaimDraft,
    source_ref: SourceRef,
    *,
    allowed_evidence_fragment_ids: frozenset[str] = _NO_EVIDENCE_FRAGMENT_IDS,
) -> Claim:
    """Convert one ClaimDraft into a validated Claim under the given source_ref."""
    unknown_fragment_ids: tuple[str, ...] = tuple(
        fragment_id
        for fragment_id in dict.fromkeys(draft_claim.evidence_fragment_ids)
        if fragment_id not in allowed_evidence_fragment_ids
    )
    if unknown_fragment_ids:
        raise UnknownEvidenceFragmentError(draft_claim.claim_id, unknown_fragment_ids)
    tier: SignalTier = SignalTier(draft_claim.tier)
    return Claim(
        claim_id=draft_claim.claim_id,
        tier=tier,
        claim=draft_claim.claim,
        category=ClaimCategory(draft_claim.category),
        tickers_affected=normalize_tickers(draft_claim.tickers_affected),
        requires_validation=is_validation_required(tier),
        source_ref=source_ref,
        cited_sources=draft_claim.cited_sources,
        evidence_fragment_ids=draft_claim.evidence_fragment_ids,
    )


def signal_set_from_draft(
    draft: SignalSetDraft,
    *,
    slug: str,
    source_ref: SourceRef,
    allowed_evidence_fragment_ids: frozenset[str] = _NO_EVIDENCE_FRAGMENT_IDS,
) -> SignalSet:
    """Assemble a SignalSet from a SignalSetDraft under the given slug and source_ref."""
    claims: list[Claim] = [
        claim_from_draft(
            claim,
            source_ref,
            allowed_evidence_fragment_ids=allowed_evidence_fragment_ids,
        )
        for claim in draft.claims
    ]
    return SignalSet(
        slug=slug,
        source_ref=source_ref,
        summary=draft.summary,
        claims=claims,
        tickers_mentioned=normalize_tickers(draft.tickers_mentioned),
        sectors_mentioned=draft.sectors_mentioned,
        macro_themes=draft.macro_themes,
        has_actionable_content=has_actionable_content(claims),
    )
