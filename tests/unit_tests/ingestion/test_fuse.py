"""Unit tests for money_pit.ingestion.fuse — assembly of ingestion artifacts into a VideoPayload."""

import pytest
from pydantic import ValidationError

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.adapters.video_llm import VideoPayload
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.ingestion.fuse import build_video_payload
from money_pit.schemas.provenance import SourceRef


@pytest.fixture
def transcript_result() -> TranscriptResult:
    return TranscriptResult(
        text="NVDA is our top pick for AI infrastructure exposure.",
        source=TranscriptSource.UPLOADER_CAPTIONS,
        has_word_timestamps=False,
    )


@pytest.fixture
def extractions() -> list[OnScreenExtraction]:
    return [
        OnScreenExtraction(
            locator="00:00:05",
            on_screen_text=["NVDA +12%", "Chart footer: data as of Q3 2026"],
            cited_sources=["Bloomberg"],
        ),
        OnScreenExtraction(
            locator="00:00:12",
            on_screen_text=["AMD -3%"],
            cited_sources=["Reuters"],
        ),
        OnScreenExtraction(
            locator="00:00:05",
            on_screen_text=["Chart footer: data as of Q3 2026"],
            cited_sources=["Bloomberg"],
        ),
    ]


def test_build_video_payload_mirrors_transcript(
    source_ref: SourceRef,
    transcript_result: TranscriptResult,
    extractions: list[OnScreenExtraction],
) -> None:
    payload = build_video_payload("2026-06-18_14-30-00", source_ref, transcript_result, extractions)

    assert payload.transcript == transcript_result.text
    assert payload.transcript_source is TranscriptSource.UPLOADER_CAPTIONS
    assert payload.has_word_timestamps is False


def test_build_video_payload_flattens_on_screen_text_in_order(
    source_ref: SourceRef,
    transcript_result: TranscriptResult,
    extractions: list[OnScreenExtraction],
) -> None:
    payload = build_video_payload("2026-06-18_14-30-00", source_ref, transcript_result, extractions)

    assert payload.on_screen_text == [
        "[00:00:05] NVDA +12%",
        "[00:00:05] Chart footer: data as of Q3 2026",
        "[00:00:05] Source: Bloomberg",
        "[00:00:12] AMD -3%",
        "[00:00:12] Source: Reuters",
    ]


def test_build_video_payload_with_empty_extractions(
    source_ref: SourceRef,
    transcript_result: TranscriptResult,
) -> None:
    payload = build_video_payload("2026-06-18_14-30-00", source_ref, transcript_result, [])

    assert payload.on_screen_text == []


def test_build_video_payload_returns_frozen_payload(
    source_ref: SourceRef,
    transcript_result: TranscriptResult,
    extractions: list[OnScreenExtraction],
) -> None:
    payload = build_video_payload("2026-06-18_14-30-00", source_ref, transcript_result, extractions)

    assert isinstance(payload, VideoPayload)
    with pytest.raises(ValidationError):
        payload.transcript = "mutated"
