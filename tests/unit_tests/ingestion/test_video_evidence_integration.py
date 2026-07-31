"""Integration tests for timestamped transcript and frame evidence preservation."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.config import Config
from money_pit.ingestion import on_screen
from money_pit.ingestion.artifacts import CaptionSegment
from money_pit.ingestion.artifacts import Keyframe
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.ingestion.evidence import build_evidence_documents
from money_pit.ingestion.on_screen import OnScreenDraft
from money_pit.ingestion.on_screen import make_on_screen_extractor
from money_pit.ingestion.transcribe import _whisper_segment
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


@pytest.fixture
def source_ref() -> SourceRef:
    return SourceRef(
        source_id="yt:evidence",
        source_type=SourceType.NARRATED_VIDEO,
        title="Evidence",
        url="https://youtu.be/evidence",
        published_at=None,
        retrieved_at="2026-07-29T14:00:00Z",
        locator=None,
    )


@dataclass
class _WhisperSegment:
    text: str
    start: float
    end: float


def test__whisper_segment_with_text_preserves_timestamps() -> None:
    result = _whisper_segment(_WhisperSegment(text=" evidence ", start=1.25, end=2.5))

    assert result == CaptionSegment(start=1.25, end=2.5, text="evidence")


def test__whisper_segment_with_blank_text_returns_none() -> None:
    assert _whisper_segment(_WhisperSegment(text="  ", start=1.25, end=2.5)) is None


def test_build_evidence_documents_with_naive_retrieval_timestamp_raises(
    source_ref: SourceRef,
) -> None:
    naive_source = source_ref.model_copy(update={"retrieved_at": "2026-07-29T14:00:00"})
    transcript = TranscriptResult(
        text="evidence",
        source=TranscriptSource.WHISPER,
        has_word_timestamps=True,
    )

    with pytest.raises(ValueError, match="must include a timezone"):
        _ = build_evidence_documents(naive_source, transcript, [])


def test_build_evidence_documents_with_audio_path_uses_audio_media_type(
    source_ref: SourceRef,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "audio.m4a"
    audio_path.write_bytes(b"audio")
    transcript = TranscriptResult(
        text="evidence",
        source=TranscriptSource.WHISPER,
        has_word_timestamps=True,
        artifact_path=audio_path,
    )

    document = build_evidence_documents(source_ref, transcript, [])[0]

    assert document.asset.media_type == "audio/*"


def test_build_evidence_documents_with_invalid_frame_locator_falls_back_to_zero(
    source_ref: SourceRef,
) -> None:
    transcript = TranscriptResult(
        text="evidence",
        source=TranscriptSource.UPLOADER_CAPTIONS,
        has_word_timestamps=False,
    )
    extraction = OnScreenExtraction(
        locator="not-a-time",
        on_screen_text=["chart"],
        cited_sources=[],
    )

    frame_document = build_evidence_documents(source_ref, transcript, [extraction])[1]

    assert frame_document.fragments[0].locator.start_seconds == 0.0


class _AgentResult:
    output = OnScreenDraft(
        on_screen_text=["NVDA +3%"],
        cited_sources=["Bloomberg"],
        bounding_box=(0.1, 0.2, 0.8, 0.9),
        confidence=0.9,
    )


class _FakeAgent:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.calls: list[object] = []

    def run_sync(self, content: object) -> _AgentResult:
        self.calls.append(content)
        return _AgentResult()


def test_make_on_screen_extractor_preserves_frame_provenance(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(on_screen, "Agent", _FakeAgent)
    image_path = tmp_path / "frame.png"
    image_path.write_bytes(b"png")
    config = Config(
        alpaca_service="alpaca",
        alpaca_username="key",
        alpaca_paper=True,
        llm_model="vision-model",
    )
    keyframe = Keyframe(timestamp=72.0, image_path=image_path, locator="00:01:12")

    extraction = make_on_screen_extractor(config)([keyframe])[0]

    assert (
        extraction.timestamp,
        extraction.image_path,
        extraction.extraction_model,
        extraction.bounding_box,
        extraction.confidence,
    ) == (72.0, image_path, "vision-model", (0.1, 0.2, 0.8, 0.9), 0.9)
