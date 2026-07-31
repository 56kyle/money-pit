"""Integration test for the lazy faster-whisper transcription boundary."""

import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.config import Config
from money_pit.ingestion.transcribe import make_faster_whisper_transcriber


@dataclass
class _Segment:
    text: str
    start: float
    end: float


class _WhisperModel:
    def __init__(
        self,
        _model: str,
        *,
        device: str,
        compute_type: str,
    ) -> None:
        _ = (device, compute_type)

    def transcribe(
        self,
        _audio_path: str,
        *,
        word_timestamps: bool,
    ) -> tuple[list[_Segment], object]:
        assert word_timestamps is True
        return (
            [
                _Segment(" first ", 0.0, 1.0),
                _Segment(" ", 1.0, 1.5),
                _Segment("second", 1.5, 2.0),
            ],
            object(),
        )


def test_make_faster_whisper_transcriber_preserves_segments(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=_WhisperModel),
    )
    config = Config(
        alpaca_service="alpaca",
        alpaca_username="key",
        alpaca_paper=True,
    )
    audio_path = tmp_path / "audio.m4a"

    result = make_faster_whisper_transcriber(config)(audio_path)

    assert result.source is TranscriptSource.WHISPER
    assert result.text == "first second"
    assert [segment.text for segment in result.segments] == ["first", "second"]
    assert result.artifact_path == audio_path
