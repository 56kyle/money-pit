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


def normalize_tickers(raw_tickers: list[str]) -> list[str]:
    """Normalize each raw ticker, dropping any that normalize to None."""
    return [t for t in (normalize_ticker(raw) for raw in raw_tickers) if t is not None]


def claim_from_draft(draft_claim: ClaimDraft, source_ref: SourceRef) -> Claim:
    """Convert one ClaimDraft into a validated Claim under the given source_ref."""
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
    )


def signal_set_from_draft(
    draft: SignalSetDraft,
    *,
    slug: str,
    source_ref: SourceRef,
) -> SignalSet:
    """Assemble a SignalSet from a SignalSetDraft under the given slug and source_ref."""
    claims: list[Claim] = [claim_from_draft(c, source_ref) for c in draft.claims]
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
