"""VideoAdapter: accepts a pre-built VideoPayload, persists it before LLM classification, and post-processes SignalSetDraft → SignalSet."""
from collections.abc import Callable
from pathlib import Path

from typing_extensions import override

from money_pit.adapters.base import SourceAdapter
from money_pit.adapters.video_llm import VideoPayload
from money_pit.compute.signal_flags import has_actionable_content
from money_pit.compute.signal_flags import normalize_ticker
from money_pit.compute.signal_flags import requires_validation as is_validation_required
from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.signal_draft import ClaimDraft
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.schemas.signals import Claim
from money_pit.schemas.signals import SignalSet


_PAYLOAD_FILENAME: str = "video_payload.json"


class VideoAdapter(SourceAdapter[VideoPayload]):
    """Concrete SourceAdapter for narrated video sources."""

    _agent: Callable[[VideoPayload], SignalSetDraft]
    _cache_dir: Path

    def __init__(
        self,
        agent: Callable[[VideoPayload], SignalSetDraft],
        cache_dir: Path,
    ) -> None:
        """Store the LLM agent callable and the payload cache directory."""
        self._agent = agent
        self._cache_dir = cache_dir

    @override
    def process(self, payload: VideoPayload) -> SignalSet:
        """Persist the payload, call the LLM agent, and post-process the draft into a SignalSet."""
        source_id = payload.source_ref.source_id
        payload_dir = self._cache_dir / source_id
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = payload_dir / _PAYLOAD_FILENAME
        _ = payload_path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")

        draft = self._agent(payload)
        claims = [_to_claim(c, payload) for c in draft.claims]

        return SignalSet(
            slug=payload.slug,
            source_ref=payload.source_ref,
            summary=draft.summary,
            claims=claims,
            tickers_mentioned=_normalize_tickers(draft.tickers_mentioned),
            sectors_mentioned=draft.sectors_mentioned,
            macro_themes=draft.macro_themes,
            has_actionable_content=has_actionable_content(claims),
        )


def _to_claim(draft_claim: ClaimDraft, payload: VideoPayload) -> Claim:
    tier = SignalTier(draft_claim.tier)
    return Claim(
        claim_id=draft_claim.claim_id,
        tier=tier,
        claim=draft_claim.claim,
        category=ClaimCategory(draft_claim.category),
        tickers_affected=_normalize_tickers(draft_claim.tickers_affected),
        requires_validation=is_validation_required(tier),
        source_ref=payload.source_ref,
        cited_sources=draft_claim.cited_sources,
    )


def _normalize_tickers(raw_tickers: list[str]) -> list[str]:
    return [t for t in (normalize_ticker(raw) for raw in raw_tickers) if t is not None]
