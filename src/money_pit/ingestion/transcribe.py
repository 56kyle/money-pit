"""Module containing the faster-whisper transcription fallback seam for the money_pit package."""

from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol
from typing import TypeAlias

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.config import Config
from money_pit.ingestion.artifacts import CaptionSegment
from money_pit.ingestion.artifacts import TranscriptResult


class WhisperSegment(Protocol):
    """Structural boundary for faster-whisper segment values."""

    text: str
    start: float
    end: float


Transcriber: TypeAlias = Callable[[Path], TranscriptResult]

_SEGMENT_JOINER: str = " "


def _whisper_segments_to_transcript(segments: Iterable[WhisperSegment]) -> str:
    """Join non-empty, stripped `.text` of each whisper segment into a single-space-separated transcript."""
    return _SEGMENT_JOINER.join(seg.text.strip() for seg in segments if seg.text.strip())


def _whisper_segment(segment: WhisperSegment) -> CaptionSegment | None:
    """Convert one faster-whisper segment into timestamped evidence."""
    text: str = segment.text.strip()
    if not text:
        return None
    return CaptionSegment(
        start=float(segment.start),
        end=float(segment.end),
        text=text,
    )


def make_faster_whisper_transcriber(config: Config) -> Transcriber:
    """Return a Transcriber that lazily loads faster-whisper and transcribes an audio file with word timestamps."""

    def transcribe(audio_path: Path) -> TranscriptResult:  # pragma: no cover
        from faster_whisper import WhisperModel

        model = WhisperModel(
            config.whisper_model,
            device=config.whisper_device,
            compute_type=config.whisper_compute_type,
        )
        segment_stream, _info = model.transcribe(str(audio_path), word_timestamps=True)
        raw_segments: list[WhisperSegment] = list(segment_stream)
        timestamped_segments: tuple[CaptionSegment, ...] = tuple(
            converted for segment in raw_segments if (converted := _whisper_segment(segment)) is not None
        )
        return TranscriptResult(
            text=_whisper_segments_to_transcript(raw_segments),
            source=TranscriptSource.WHISPER,
            has_word_timestamps=True,
            segments=timestamped_segments,
            artifact_path=audio_path,
        )

    return transcribe
