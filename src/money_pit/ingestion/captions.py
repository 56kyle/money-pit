"""Module containing pure WebVTT parsing and the caption-selection cascade for the money_pit package."""

import re

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.ingestion.artifacts import CaptionSegment
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.ingestion.artifacts import VideoArtifacts


_WEBVTT_HEADER: str = "WEBVTT"
_NOTE_KEYWORD: str = "NOTE"
_STYLE_KEYWORD: str = "STYLE"
_TIMING_ARROW: str = "-->"
_TIMESTAMP_SEPARATOR: str = ":"
_SECONDS_PER_MINUTE: int = 60
_SECONDS_PER_HOUR: int = 3600
_HOUR_MINUTE_SECOND_PARTS: int = 3
_MINUTE_SECOND_PARTS: int = 2
_TRANSCRIPT_JOINER: str = " "

_INLINE_TAG_PATTERN: re.Pattern[str] = re.compile(r"<[^>]*>")
_WHITESPACE_PATTERN: re.Pattern[str] = re.compile(r"\s+")


def _parse_timestamp(token: str) -> float:
    """Parse a WebVTT HH:MM:SS.mmm or MM:SS.mmm timestamp token into float seconds."""
    parts = token.split(_TIMESTAMP_SEPARATOR)
    if len(parts) == _HOUR_MINUTE_SECOND_PARTS:
        hours, minutes, seconds = parts
        return int(hours) * _SECONDS_PER_HOUR + int(minutes) * _SECONDS_PER_MINUTE + float(seconds)
    if len(parts) == _MINUTE_SECOND_PARTS:
        minutes, seconds = parts
        return int(minutes) * _SECONDS_PER_MINUTE + float(seconds)
    raise ValueError(f"Unparseable WebVTT timestamp: {token!r}")


def _clean_text(text_lines: list[str]) -> str:
    """Strip inline tags from cue text lines and collapse whitespace into single spaces."""
    joined = _TRANSCRIPT_JOINER.join(text_lines)
    without_tags = _INLINE_TAG_PATTERN.sub("", joined)
    return _WHITESPACE_PATTERN.sub(_TRANSCRIPT_JOINER, without_tags).strip()


def _split_blocks(text: str) -> list[list[str]]:
    """Split a WebVTT document into blocks of non-blank lines separated by blank lines."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        if raw_line.strip():
            current.append(raw_line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _is_ignored_block(block: list[str]) -> bool:
    """Report whether a block is the WEBVTT header or a NOTE/STYLE block to be skipped."""
    first = block[0].strip()
    return (
        first == _WEBVTT_HEADER
        or first.startswith(_WEBVTT_HEADER)
        or first == _NOTE_KEYWORD
        or first.startswith(f"{_NOTE_KEYWORD} ")
        or first == _STYLE_KEYWORD
        or first.startswith(f"{_STYLE_KEYWORD} ")
    )


def _parse_cue(block: list[str]) -> CaptionSegment | None:
    """Parse a single cue block into a CaptionSegment, or None if it has no timing or no text."""
    timing_index = next((index for index, line in enumerate(block) if _TIMING_ARROW in line), None)
    if timing_index is None:
        return None
    start_side, _, end_side = block[timing_index].partition(_TIMING_ARROW)
    start = _parse_timestamp(start_side.split()[0])
    end = _parse_timestamp(end_side.split()[0])
    text = _clean_text(block[timing_index + 1 :])
    if not text:
        return None
    return CaptionSegment(start=start, end=end, text=text)


def parse_vtt(text: str) -> list[CaptionSegment]:
    """Parse a WebVTT document into cleaned caption segments, skipping empty and non-cue blocks."""
    segments: list[CaptionSegment] = []
    for block in _split_blocks(text):
        if _is_ignored_block(block):
            continue
        segment = _parse_cue(block)
        if segment is not None:
            segments.append(segment)
    return segments


def segments_to_transcript(segments: list[CaptionSegment]) -> str:
    """Join segment texts into a single transcript, skipping consecutive duplicates.

    Consecutive-duplicate skipping is a heuristic for YouTube rolling-caption duplication,
    where overlapping auto-caption cues repeat the same text across adjacent segments.
    """
    kept: list[str] = []
    for segment in segments:
        if not kept or segment.text != kept[-1]:
            kept.append(segment.text)
    return _TRANSCRIPT_JOINER.join(kept)


def select_caption_transcript(artifacts: VideoArtifacts) -> TranscriptResult | None:
    """Select the best available caption track as a transcript, or None if none exist.

    Returning None signals to the pipeline that audio transcription is still needed.
    """
    uploader_path = artifacts.uploader_caption_path
    if uploader_path is not None and uploader_path.exists():
        segments = parse_vtt(uploader_path.read_text(encoding="utf-8"))
        return TranscriptResult(
            text=segments_to_transcript(segments),
            source=TranscriptSource.UPLOADER_CAPTIONS,
            has_word_timestamps=False,
            segments=tuple(segments),
            artifact_path=uploader_path,
        )
    auto_path = artifacts.auto_caption_path
    if auto_path is not None and auto_path.exists():
        segments = parse_vtt(auto_path.read_text(encoding="utf-8"))
        return TranscriptResult(
            text=segments_to_transcript(segments),
            source=TranscriptSource.AUTO_CAPTIONS,
            has_word_timestamps=False,
            segments=tuple(segments),
            artifact_path=auto_path,
        )
    return None
