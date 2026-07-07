"""Unit tests for the VideoPayload model."""

import pytest

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.adapters.video_llm import VideoPayload
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


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


def test_video_payload_serializes_round_trip(video_payload: VideoPayload) -> None:
    """VideoPayload survives a JSON round-trip and compares equal to the original."""
    round_tripped = VideoPayload.model_validate_json(video_payload.model_dump_json())

    assert round_tripped == video_payload
