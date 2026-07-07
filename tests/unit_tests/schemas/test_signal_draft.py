"""Tests for money_pit.schemas.signal_draft — A1 draft cleanup (wave S7, theme T1).

Pins the draft→node→contract boundary for A1:
- ClaimDraft drops source_context (replaced by structured SourceRef) and
  requires_validation (tier-derived in the adapter);
- SignalSetDraft drops has_actionable_content (code-derived) and renames
  episode_summary → summary (the §2.3 contract field name).
"""

import pytest
from pydantic import ValidationError

from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import SourceType
from money_pit.schemas.signal_draft import ClaimDraft
from money_pit.schemas.signal_draft import SignalSetDraft


def _valid_claim_draft_kwargs() -> dict[str, object]:
    return dict(
        claim_id="claim_001",
        claim="NVDA data center revenue rose 112% year-over-year.",
        tier=SignalTier.HIGH.value,
        category=ClaimCategory.FUNDAMENTAL.value,
        tickers_affected=["NVDA"],
        cited_sources=[],
    )


def _valid_signal_set_draft_kwargs() -> dict[str, object]:
    return dict(
        source_id="yt_test_001",
        source_type=SourceType.NARRATED_VIDEO.value,
        title="Test Video: NVDA Thesis Review",
        url=None,
        published_at=None,
        retrieved_at="2026-06-18T14:30:00Z",
        summary="NVDA is well-positioned for AI infrastructure. Risk is AMD competition.",
        claims=[],
        tickers_mentioned=[],
        sectors_mentioned=[],
        macro_themes=[],
    )


def test_claim_draft_with_valid() -> None:
    draft = ClaimDraft(**_valid_claim_draft_kwargs())

    assert draft.claim_id == "claim_001"


def test_claim_draft_rejects_source_context() -> None:
    with pytest.raises(ValidationError):
        ClaimDraft(**_valid_claim_draft_kwargs(), source_context="who said it and in what context")


def test_claim_draft_rejects_requires_validation() -> None:
    with pytest.raises(ValidationError):
        ClaimDraft(**_valid_claim_draft_kwargs(), requires_validation=True)


def test_signal_set_draft_with_summary() -> None:
    draft = SignalSetDraft(**_valid_signal_set_draft_kwargs())

    assert draft.summary == "NVDA is well-positioned for AI infrastructure. Risk is AMD competition."


def test_signal_set_draft_rejects_has_actionable_content() -> None:
    with pytest.raises(ValidationError):
        SignalSetDraft(**_valid_signal_set_draft_kwargs(), has_actionable_content=True)


def test_signal_set_draft_rejects_episode_summary() -> None:
    kwargs = _valid_signal_set_draft_kwargs()
    del kwargs["summary"]
    with pytest.raises(ValidationError):
        SignalSetDraft(**kwargs, episode_summary="the old field name")
