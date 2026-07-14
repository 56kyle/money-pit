"""Unit tests for money_pit.ingestion.artifacts — the frozen video-ingestion intermediate models."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.ingestion.artifacts import CaptionSegment
from money_pit.ingestion.artifacts import Keyframe
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.ingestion.artifacts import VideoArtifacts
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


@pytest.fixture
def source_ref() -> SourceRef:
    return SourceRef(
        source_id="yt_test_001",
        source_type=SourceType.NARRATED_VIDEO,
        title="Test Video",
        url=None,
        published_at=None,
        retrieved_at="2026-06-18T14:30:00Z",
        locator=None,
    )


def test_caption_segment_is_frozen() -> None:
    segment = CaptionSegment(start=0.0, end=4.0, text="hello")

    with pytest.raises(ValidationError):
        segment.text = "mutated"


def test_transcript_result_is_frozen() -> None:
    result = TranscriptResult(text="hi", source=TranscriptSource.UPLOADER_CAPTIONS, has_word_timestamps=False)

    with pytest.raises(ValidationError):
        result.text = "mutated"


def test_transcript_result_round_trips() -> None:
    result = TranscriptResult(
        text="today we look at NVDA the data center story",
        source=TranscriptSource.AUTO_CAPTIONS,
        has_word_timestamps=False,
    )

    round_tripped = TranscriptResult.model_validate_json(result.model_dump_json())

    assert round_tripped == result
    assert round_tripped.source is TranscriptSource.AUTO_CAPTIONS


def test_on_screen_extraction_is_frozen() -> None:
    extraction = OnScreenExtraction(locator="00:00:05", on_screen_text=["NVDA +12%"], cited_sources=["Bloomberg"])

    with pytest.raises(ValidationError):
        extraction.locator = "00:00:10"


def test_on_screen_extraction_round_trips() -> None:
    extraction = OnScreenExtraction(
        locator="00:00:05",
        on_screen_text=["NVDA +12%", "Chart footer: data as of Q3 2026"],
        cited_sources=["Bloomberg", "Reuters"],
    )

    round_tripped = OnScreenExtraction.model_validate_json(extraction.model_dump_json())

    assert round_tripped == extraction


def test_keyframe_is_frozen() -> None:
    keyframe = Keyframe(timestamp=5.0, image_path=Path("frames/frame_0005.jpg"), locator="00:00:05")

    with pytest.raises(ValidationError):
        keyframe.locator = "00:00:10"


def test_keyframe_round_trips_path() -> None:
    keyframe = Keyframe(timestamp=5.0, image_path=Path("frames/frame_0005.jpg"), locator="00:00:05")

    round_tripped = Keyframe.model_validate_json(keyframe.model_dump_json())

    assert round_tripped == keyframe
    assert isinstance(round_tripped.image_path, Path)


def test_video_artifacts_is_frozen(source_ref: SourceRef) -> None:
    artifacts = VideoArtifacts(
        source_ref=source_ref,
        audio_path=None,
        video_path=None,
        uploader_caption_path=None,
        auto_caption_path=None,
        metadata_path=Path("meta.json"),
    )

    with pytest.raises(ValidationError):
        artifacts.metadata_path = Path("other.json")


def test_video_artifacts_round_trips_paths(source_ref: SourceRef) -> None:
    artifacts = VideoArtifacts(
        source_ref=source_ref,
        audio_path=Path("audio.m4a"),
        video_path=Path("video.mp4"),
        uploader_caption_path=Path("uploader.en.vtt"),
        auto_caption_path=None,
        metadata_path=Path("meta.json"),
    )

    round_tripped = VideoArtifacts.model_validate_json(artifacts.model_dump_json())

    assert round_tripped == artifacts
    assert isinstance(round_tripped.metadata_path, Path)
    assert isinstance(round_tripped.uploader_caption_path, Path)
    assert round_tripped.auto_caption_path is None
