"""Unit tests for money_pit.ingestion.captions — WebVTT parsing and the caption-selection cascade."""

from pathlib import Path

import pytest

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.ingestion.artifacts import CaptionSegment
from money_pit.ingestion.artifacts import VideoArtifacts
from money_pit.ingestion.captions import _parse_timestamp
from money_pit.ingestion.captions import parse_vtt
from money_pit.ingestion.captions import segments_to_transcript
from money_pit.ingestion.captions import select_caption_transcript
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


_UPLOADER_CUE_COUNT: int = 5
_AUTO_CUE_COUNT: int = 5


def test_parse_vtt_with_uploader(uploader_vtt_text: str) -> None:
    segments = parse_vtt(uploader_vtt_text)

    assert len(segments) == _UPLOADER_CUE_COUNT


def test_parse_vtt_with_uploader_first_cue_timing(uploader_vtt_text: str) -> None:
    first = parse_vtt(uploader_vtt_text)[0]

    assert first.start == 0.0
    assert first.end == 4.0


def test_parse_vtt_with_uploader_strips_markup(uploader_vtt_text: str) -> None:
    first = parse_vtt(uploader_vtt_text)[0]

    assert first.text == "Welcome back to the channel, today we cover NVDA."


def test_parse_vtt_with_header_note_and_style_blocks_dropped() -> None:
    text = "WEBVTT\n\nNOTE this is metadata\n\nSTYLE\n::cue { color: white }\n"

    assert parse_vtt(text) == []


def test_parse_vtt_with_auto_strips_inline_tags(auto_vtt_text: str) -> None:
    texts = [segment.text for segment in parse_vtt(auto_vtt_text)]

    assert all("<" not in text and ">" not in text for text in texts)
    assert "today we look at NVDA" in texts
    assert "the data center story" in texts


def test_parse_vtt_with_auto_drops_cue_settings(auto_vtt_text: str) -> None:
    segments = parse_vtt(auto_vtt_text)

    assert all("align" not in segment.text and "position" not in segment.text for segment in segments)
    assert segments[0].start == 0.0
    assert segments[0].end == 3.0


def test_parse_vtt_with_auto_drops_empty_after_strip(auto_vtt_text: str) -> None:
    segments = parse_vtt(auto_vtt_text)

    assert len(segments) == _AUTO_CUE_COUNT


def test__parse_timestamp_with_hour_minute_second() -> None:
    assert _parse_timestamp("01:02:03.500") == 3723.5


def test__parse_timestamp_with_minute_second() -> None:
    assert _parse_timestamp("02:03.500") == 123.5


def test__parse_timestamp_with_malformed() -> None:
    with pytest.raises(ValueError, match="Unparseable WebVTT timestamp"):
        _parse_timestamp("garbage")


def test_parse_vtt_with_malformed_timing_raises() -> None:
    text = "WEBVTT\n\nxx:yy --> zz:ww\nsome caption text\n"

    with pytest.raises(ValueError):
        parse_vtt(text)


def _segment(text: str) -> CaptionSegment:
    return CaptionSegment(start=0.0, end=1.0, text=text)


def test_segments_to_transcript_with_consecutive_duplicates() -> None:
    segments = [
        _segment("today we look at NVDA"),
        _segment("today we look at NVDA"),
        _segment("the data center story"),
        _segment("today we look at NVDA"),
    ]

    transcript = segments_to_transcript(segments)

    assert transcript == "today we look at NVDA the data center story today we look at NVDA"


def _artifacts(
    uploader_caption_path: Path | None,
    auto_caption_path: Path | None,
    metadata_path: Path,
) -> VideoArtifacts:
    return VideoArtifacts(
        source_ref=SourceRef(
            source_id="yt_test_001",
            source_type=SourceType.NARRATED_VIDEO,
            title="Test Video",
            url=None,
            published_at=None,
            retrieved_at="2026-06-18T14:30:00Z",
            locator=None,
        ),
        audio_path=None,
        video_path=None,
        uploader_caption_path=uploader_caption_path,
        auto_caption_path=auto_caption_path,
        metadata_path=metadata_path,
    )


def test_select_caption_transcript_with_uploader_present(
    uploader_vtt_path: Path,
    auto_vtt_path: Path,
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(uploader_vtt_path, auto_vtt_path, tmp_path / "meta.json")

    result = select_caption_transcript(artifacts)

    assert result is not None
    assert result.source is TranscriptSource.UPLOADER_CAPTIONS
    assert result.has_word_timestamps is False
    assert "NVDA" in result.text


def test_select_caption_transcript_with_only_auto_present(
    auto_vtt_path: Path,
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(None, auto_vtt_path, tmp_path / "meta.json")

    result = select_caption_transcript(artifacts)

    assert result is not None
    assert result.source is TranscriptSource.AUTO_CAPTIONS
    assert result.text.count("today we look at NVDA") == 1


def test_select_caption_transcript_with_uploader_path_absent_falls_through(
    auto_vtt_path: Path,
    tmp_path: Path,
) -> None:
    missing_uploader = tmp_path / "does_not_exist.en.vtt"
    artifacts = _artifacts(missing_uploader, auto_vtt_path, tmp_path / "meta.json")

    result = select_caption_transcript(artifacts)

    assert result is not None
    assert result.source is TranscriptSource.AUTO_CAPTIONS


def test_select_caption_transcript_with_neither_present(tmp_path: Path) -> None:
    artifacts = _artifacts(None, tmp_path / "missing.vtt", tmp_path / "meta.json")

    assert select_caption_transcript(artifacts) is None
