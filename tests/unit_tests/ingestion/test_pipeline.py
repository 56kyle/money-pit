"""Unit tests for money_pit.ingestion.pipeline — the per-stage-cached ingestion orchestrator.

Every stage is driven through an all-fake IngestionSeams so the whole pipeline runs pure and offline;
the only real I/O is the committed uploader caption fixture and the per-test tmp_path cache dir.
"""

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import pytest

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.adapters.video_llm import VideoPayload
from money_pit.ingestion.artifacts import Keyframe
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.ingestion.artifacts import VideoArtifacts
from money_pit.ingestion.keyframes import _seconds_to_locator
from money_pit.ingestion.pipeline import IngestionError
from money_pit.ingestion.pipeline import IngestionSeams
from money_pit.ingestion.pipeline import _cached_list
from money_pit.ingestion.pipeline import _cached_model
from money_pit.ingestion.pipeline import _resolve_keyframes
from money_pit.ingestion.pipeline import _resolve_transcript
from money_pit.ingestion.pipeline import _source_id_from_url
from money_pit.ingestion.pipeline import ingest_video
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


_URL: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s"
_SHORT_URL: str = "https://youtu.be/dQw4w9WgXcQ?si=x"
_SOURCE_ID: str = "yt:dQw4w9WgXcQ"
_SLUG: str = "2026-06-18_14-30-00"
_MAX_FRAMES: int = 40
_GOLDEN_NAME: str = "expected_video_payload.json"

_EXPECTED_TRANSCRIPT: str = (
    "Welcome back to the channel, today we cover NVDA. "
    "The data center segment continues to show strong growth. "
    "Analysts remain bullish heading into earnings. "
    "Source: Bloomberg "
    "NVDA is our top pick for AI infrastructure exposure."
)
_EXPECTED_ON_SCREEN: list[str] = [
    "[00:00:01] NVDA chart",
    "[00:00:01] Source: Bloomberg",
    "[00:00:05] NVDA chart",
    "[00:00:05] Source: Bloomberg",
]


@dataclass
class _SeamCalls:
    """Per-seam invocation counter shared by a single fake IngestionSeams bundle."""

    downloader: int = 0
    transcriber: int = 0
    detector: int = 0
    extractor: int = 0
    on_screen: int = 0


@dataclass
class _FakeSeams:
    """A fully offline IngestionSeams paired with the call counter its stages increment."""

    seams: IngestionSeams
    calls: _SeamCalls = field(default_factory=_SeamCalls)


def _source_ref(url: str) -> SourceRef:
    return SourceRef(
        source_id=_source_id_from_url(url),
        source_type=SourceType.NARRATED_VIDEO,
        title="T",
        url=url,
        published_at="2026-06-18T00:00:00Z",
        retrieved_at="2026-06-18T14:30:00Z",
        locator=None,
    )


def _artifacts(
    uploader_caption_path: Path | None,
    *,
    audio_path: Path | None = None,
    video_path: Path | None = Path("v.mp4"),
    auto_caption_path: Path | None = None,
) -> VideoArtifacts:
    return VideoArtifacts(
        source_ref=_source_ref(_URL),
        audio_path=audio_path,
        video_path=video_path,
        uploader_caption_path=uploader_caption_path,
        auto_caption_path=auto_caption_path,
        metadata_path=Path("metadata.json"),
    )


def _make_fake_seams(uploader_caption_path: Path | None) -> _FakeSeams:
    """Build an all-fake IngestionSeams whose stages record their calls and never touch the network."""
    calls = _SeamCalls()

    def fake_downloader(url: str, out_dir: Path) -> VideoArtifacts:
        calls.downloader += 1
        return VideoArtifacts(
            source_ref=_source_ref(url),
            audio_path=None,
            video_path=Path("v.mp4"),
            uploader_caption_path=uploader_caption_path,
            auto_caption_path=None,
            metadata_path=out_dir / "metadata.json",
        )

    def fake_transcriber(audio: Path) -> TranscriptResult:
        _ = audio
        calls.transcriber += 1
        return TranscriptResult(text="whisper transcript", source=TranscriptSource.WHISPER, has_word_timestamps=True)

    def fake_detector(video: Path) -> list[float]:
        _ = video
        calls.detector += 1
        return [1.0, 5.0]

    def fake_extractor(video: Path, timestamps: list[float], out_dir: Path) -> list[Keyframe]:
        _ = video
        calls.extractor += 1
        return [
            Keyframe(timestamp=timestamp, image_path=out_dir / f"kf_{index}.png", locator=_seconds_to_locator(timestamp))
            for index, timestamp in enumerate(timestamps)
        ]

    def fake_on_screen(keyframes: list[Keyframe]) -> list[OnScreenExtraction]:
        calls.on_screen += 1
        return [
            OnScreenExtraction(locator=keyframe.locator, on_screen_text=["NVDA chart"], cited_sources=["Bloomberg"])
            for keyframe in keyframes
        ]

    seams = IngestionSeams(
        downloader=fake_downloader,
        transcriber=fake_transcriber,
        detector=fake_detector,
        extractor=fake_extractor,
        on_screen=fake_on_screen,
    )
    return _FakeSeams(seams=seams, calls=calls)


@pytest.mark.parametrize("url", [_URL, _SHORT_URL])
def test__source_id_from_url_with_valid(url: str) -> None:
    assert _source_id_from_url(url) == _SOURCE_ID


def test__source_id_from_url_with_invalid() -> None:
    with pytest.raises(ValueError):
        _source_id_from_url("https://example.com/not-a-video")


def test__cached_model(tmp_path: Path) -> None:
    path = tmp_path / "transcript.json"
    calls = 0

    def compute() -> TranscriptResult:
        nonlocal calls
        calls += 1
        return TranscriptResult(text="hello", source=TranscriptSource.WHISPER, has_word_timestamps=True)

    first = _cached_model(path, TranscriptResult, compute)
    assert calls == 1
    assert path.exists()

    second = _cached_model(path, TranscriptResult, compute)
    assert calls == 1
    assert second == first


def test__cached_list(tmp_path: Path) -> None:
    path = tmp_path / "on_screen.json"
    calls = 0

    def compute() -> list[OnScreenExtraction]:
        nonlocal calls
        calls += 1
        return [OnScreenExtraction(locator="00:00:01", on_screen_text=["NVDA chart"], cited_sources=["Bloomberg"])]

    first = _cached_list(path, OnScreenExtraction, compute)
    assert calls == 1
    assert path.exists()

    second = _cached_list(path, OnScreenExtraction, compute)
    assert calls == 1
    assert second == first


def test_ingest_video_with_uploader_captions(tmp_path: Path, uploader_vtt_path: Path) -> None:
    fake = _make_fake_seams(uploader_vtt_path)

    result = ingest_video(_URL, slug=_SLUG, cache_dir=tmp_path, seams=fake.seams, max_frames=_MAX_FRAMES)

    assert result.transcript_source is TranscriptSource.UPLOADER_CAPTIONS
    assert result.has_word_timestamps is False
    assert result.transcript == _EXPECTED_TRANSCRIPT
    assert result.on_screen_text == _EXPECTED_ON_SCREEN
    assert result.source_ref.source_id == _SOURCE_ID
    assert fake.calls.transcriber == 0


def test_ingest_video_matches_golden(tmp_path: Path, uploader_vtt_path: Path, video_data_folder: Path) -> None:
    fake = _make_fake_seams(uploader_vtt_path)

    result = ingest_video(_URL, slug=_SLUG, cache_dir=tmp_path, seams=fake.seams, max_frames=_MAX_FRAMES)

    golden = video_data_folder / _GOLDEN_NAME
    assert VideoPayload.model_validate_json(golden.read_text(encoding="utf-8")) == result


def test_ingest_video_second_run_skips_all_seams(tmp_path: Path, uploader_vtt_path: Path) -> None:
    first_fake = _make_fake_seams(uploader_vtt_path)
    first = ingest_video(_URL, slug=_SLUG, cache_dir=tmp_path, seams=first_fake.seams, max_frames=_MAX_FRAMES)

    second_fake = _make_fake_seams(uploader_vtt_path)
    second = ingest_video(_URL, slug=_SLUG, cache_dir=tmp_path, seams=second_fake.seams, max_frames=_MAX_FRAMES)

    assert second_fake.calls == _SeamCalls()
    assert second == first


def test__resolve_transcript_with_no_captions_and_no_audio(uploader_vtt_path: Path) -> None:
    fake = _make_fake_seams(uploader_vtt_path)
    artifacts = _artifacts(uploader_caption_path=None, audio_path=None)

    with pytest.raises(IngestionError):
        _resolve_transcript(artifacts, fake.seams)


def test__resolve_transcript_with_audio_and_no_captions(uploader_vtt_path: Path) -> None:
    fake = _make_fake_seams(uploader_vtt_path)
    artifacts = _artifacts(uploader_caption_path=None, audio_path=Path("media.m4a"))

    result = _resolve_transcript(artifacts, fake.seams)

    assert fake.calls.transcriber == 1
    assert result.source is TranscriptSource.WHISPER


def test__resolve_keyframes_with_no_video_returns_empty(tmp_path: Path, uploader_vtt_path: Path) -> None:
    fake = _make_fake_seams(uploader_vtt_path)
    artifacts = _artifacts(uploader_caption_path=uploader_vtt_path, video_path=None)

    result = _resolve_keyframes(artifacts, tmp_path, fake.seams, _MAX_FRAMES)

    assert result == []
    assert fake.calls.detector == 0
    assert fake.calls.extractor == 0
