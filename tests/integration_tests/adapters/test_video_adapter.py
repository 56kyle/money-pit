"""Integration tests for VideoAdapter: VideoPayload → persist → stub agent → SignalSet."""
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from money_pit.adapters.video import VideoAdapter
from money_pit.adapters.video_llm import TranscriptSource, VideoPayload
from money_pit.schemas.enums import ClaimCategory, SignalTier, SourceType
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signal_draft import ClaimDraft, SignalSetDraft
from money_pit.schemas.signals import SignalSet


@pytest.fixture
def source_ref() -> SourceRef:
    return SourceRef(
        source_id="yt_test_001",
        source_type=SourceType.NARRATED_VIDEO,
        title="Test Video: NVDA Thesis Review",
        url="https://www.youtube.com/watch?v=test",
        published_at="2026-01-15T12:00:00Z",
        retrieved_at="2026-06-18T14:30:00Z",
        locator=None,
    )


@pytest.fixture
def video_payload(source_ref: SourceRef) -> VideoPayload:
    return VideoPayload(
        slug="2026-06-18_14-30-00",
        source_ref=source_ref,
        transcript=(
            "NVDA is well-positioned for AI infrastructure buildout. "
            "The data center segment continues to show 200%+ growth. "
            "Risk: AMD competition increasing."
        ),
        transcript_source=TranscriptSource.UPLOADER_CAPTIONS,
        has_word_timestamps=False,
        on_screen_text=[],
    )


def _high_claim() -> ClaimDraft:
    return ClaimDraft(
        claim_id="claim_001",
        claim="NVDA data center segment shows 200%+ YoY growth driven by AI infrastructure buildout.",
        source_context="data center segment continues to show 200%+ growth",
        tier=SignalTier.HIGH.value,
        category=ClaimCategory.FUNDAMENTAL.value,
        tickers_affected=["NVDA"],
        requires_validation=True,
        cited_sources=[],
    )


def _low_claim() -> ClaimDraft:
    return ClaimDraft(
        claim_id="claim_002",
        claim="AMD competition is increasing in the GPU space.",
        source_context="AMD competition increasing",
        tier=SignalTier.LOW.value,
        category=ClaimCategory.SENTIMENT.value,
        tickers_affected=["AMD"],
        requires_validation=False,
        cited_sources=[],
    )


def _make_draft(source_ref: SourceRef, claims: list[ClaimDraft]) -> SignalSetDraft:
    return SignalSetDraft(
        source_id=source_ref.source_id,
        source_type=source_ref.source_type.value,
        title=source_ref.title,
        url=source_ref.url,
        published_at=source_ref.published_at,
        retrieved_at=source_ref.retrieved_at,
        episode_summary="NVDA is well-positioned for AI infrastructure. Risk is AMD competition.",
        claims=claims,
        tickers_mentioned=["NVDA", "AMD"],
        sectors_mentioned=["Technology", "Semiconductors"],
        macro_themes=["AI infrastructure buildout"],
        has_actionable_content=True,
    )


@pytest.fixture
def stub_agent(source_ref: SourceRef) -> Callable[[VideoPayload], SignalSetDraft]:
    def _agent(_payload: VideoPayload) -> SignalSetDraft:
        return _make_draft(source_ref, [_high_claim(), _low_claim()])

    return _agent


@pytest.fixture
def low_tier_stub_agent(source_ref: SourceRef) -> Callable[[VideoPayload], SignalSetDraft]:
    def _agent(_payload: VideoPayload) -> SignalSetDraft:
        return _make_draft(source_ref, [_low_claim()])

    return _agent


@pytest.fixture
def empty_ticker_stub_agent(source_ref: SourceRef) -> Callable[[VideoPayload], SignalSetDraft]:
    def _agent(_payload: VideoPayload) -> SignalSetDraft:
        claim_with_empty_ticker = ClaimDraft(
            claim_id="claim_003",
            claim="General market sentiment is positive.",
            source_context="general positive outlook",
            tier=SignalTier.LOW.value,
            category=ClaimCategory.SENTIMENT.value,
            tickers_affected=[""],
            requires_validation=False,
            cited_sources=[],
        )
        return _make_draft(source_ref, [claim_with_empty_ticker])

    return _agent


def test_video_adapter_process_returns_signal_set(
    stub_agent: Callable[[VideoPayload], SignalSetDraft],
    video_payload: VideoPayload,
    tmp_path: Path,
) -> None:
    """VideoAdapter.process returns a well-formed SignalSet with correct identity fields."""
    result = VideoAdapter(agent=stub_agent, cache_dir=tmp_path).process(video_payload)

    assert isinstance(result, SignalSet)
    assert result.source_ref.source_id == "yt_test_001"
    assert result.slug == "2026-06-18_14-30-00"
    assert len(result.claims) > 0


def test_video_adapter_persists_payload_before_llm(
    stub_agent: Callable[[VideoPayload], SignalSetDraft],
    video_payload: VideoPayload,
    tmp_path: Path,
) -> None:
    """VideoAdapter writes a video_payload.json under cache_dir/source_id/ before calling the agent."""
    _ = VideoAdapter(agent=stub_agent, cache_dir=tmp_path).process(video_payload)

    payload_path = tmp_path / "yt_test_001" / "video_payload.json"
    assert payload_path.exists()
    parsed = cast(dict[str, object], json.loads(payload_path.read_text(encoding="utf-8")))
    source_ref_obj = parsed["source_ref"]
    assert isinstance(source_ref_obj, dict)
    assert "source_id" in source_ref_obj


def test_video_adapter_has_actionable_content_with_high_tier_claim(
    stub_agent: Callable[[VideoPayload], SignalSetDraft],
    video_payload: VideoPayload,
    tmp_path: Path,
) -> None:
    """A SignalSet containing a HIGH-tier claim has has_actionable_content=True."""
    result = VideoAdapter(agent=stub_agent, cache_dir=tmp_path).process(video_payload)

    assert result.has_actionable_content is True


def test_video_adapter_has_actionable_content_false_for_low_tier_only(
    low_tier_stub_agent: Callable[[VideoPayload], SignalSetDraft],
    video_payload: VideoPayload,
    tmp_path: Path,
) -> None:
    """A SignalSet containing only LOW-tier claims has has_actionable_content=False."""
    result = VideoAdapter(agent=low_tier_stub_agent, cache_dir=tmp_path).process(video_payload)

    assert result.has_actionable_content is False


def test_video_adapter_filters_invalid_tickers(
    empty_ticker_stub_agent: Callable[[VideoPayload], SignalSetDraft],
    video_payload: VideoPayload,
    tmp_path: Path,
) -> None:
    """Empty-string tickers in ClaimDraft are dropped by normalize_ticker and absent from the Claim."""
    result = VideoAdapter(agent=empty_ticker_stub_agent, cache_dir=tmp_path).process(video_payload)

    assert len(result.claims) == 1
    assert result.claims[0].tickers_affected == []


def test_video_payload_serializes_round_trip(video_payload: VideoPayload) -> None:
    """VideoPayload survives a JSON round-trip and compares equal to the original."""
    round_tripped = VideoPayload.model_validate_json(video_payload.model_dump_json())

    assert round_tripped == video_payload
