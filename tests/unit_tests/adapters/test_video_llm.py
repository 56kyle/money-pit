"""Unit tests for the VideoPayload model."""

import pytest
from _pytest.fixtures import FixtureRequest

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.adapters.video_llm import VideoPayload
from money_pit.adapters.video_llm import _build_user_message
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
def video_payload__on_screen_text(request: FixtureRequest) -> list[str]:
    return getattr(request, "param", [])


@pytest.fixture
def video_payload(source_ref: SourceRef, video_payload__on_screen_text: list[str]) -> VideoPayload:
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
        on_screen_text=video_payload__on_screen_text,
    )


def test_video_payload_serializes_round_trip(video_payload: VideoPayload) -> None:
    """VideoPayload survives a JSON round-trip and compares equal to the original."""
    round_tripped = VideoPayload.model_validate_json(video_payload.model_dump_json())

    assert round_tripped == video_payload


def test_video_payload_loads_legacy_cache_without_evidence(source_ref: SourceRef) -> None:
    legacy_payload = VideoPayload.model_validate(
        {
            "slug": "legacy",
            "source_ref": source_ref.model_dump(mode="json"),
            "transcript": "Legacy cached transcript.",
            "transcript_source": "uploader_captions",
            "has_word_timestamps": False,
            "on_screen_text": [],
        }
    )

    assert legacy_payload.evidence_assets == ()
    assert legacy_payload.evidence_fragments == ()


@pytest.mark.parametrize(
    "video_payload__on_screen_text",
    [["[00:00:05] NVDA chart", "[00:00:05] Source: Bloomberg"]],
    indirect=True,
)
def test__build_user_message_with_on_screen_text_present(video_payload: VideoPayload) -> None:
    """The On-Screen Text block renders its lines and Transcript Provenance follows it."""
    message = _build_user_message(video_payload)

    assert "## On-Screen Text" in message
    assert "[00:00:05] NVDA chart" in message
    assert "[00:00:05] Source: Bloomberg" in message
    assert "## Transcript Provenance" in message
    assert video_payload.transcript_source.value in message
    assert "word-level timestamps: False" in message


def test__build_user_message_with_on_screen_text_absent(video_payload: VideoPayload) -> None:
    """An empty on_screen_text omits the On-Screen Text block but keeps Transcript and Provenance."""
    message = _build_user_message(video_payload)

    assert "## On-Screen Text" not in message
    assert "## Transcript\n" in message
    assert "## Transcript Provenance" in message
