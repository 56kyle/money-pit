"""Module containing the per-stage-cached ingestion orchestrator that wires the video-ingestion seams for the money_pit package."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qs
from urllib.parse import urlparse

from pydantic import BaseModel
from pydantic import TypeAdapter

from money_pit.adapters.video_llm import VideoPayload
from money_pit.config import Config
from money_pit.constants import source_id_to_dirname
from money_pit.ingestion.artifacts import Keyframe
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.ingestion.artifacts import VideoArtifacts
from money_pit.ingestion.captions import select_caption_transcript
from money_pit.ingestion.fetch import Downloader
from money_pit.ingestion.fetch import make_ytdlp_downloader
from money_pit.ingestion.fuse import build_video_payload
from money_pit.ingestion.keyframes import FrameExtractor
from money_pit.ingestion.keyframes import SceneDetector
from money_pit.ingestion.keyframes import make_opencv_extractor
from money_pit.ingestion.keyframes import make_scenedetect_detector
from money_pit.ingestion.keyframes import select_keyframes
from money_pit.ingestion.on_screen import OnScreenExtractor
from money_pit.ingestion.on_screen import make_on_screen_extractor
from money_pit.ingestion.transcribe import Transcriber
from money_pit.ingestion.transcribe import make_faster_whisper_transcriber


T = TypeVar("T", bound=BaseModel)
M = TypeVar("M", bound=BaseModel)

_SOURCE_ID_PREFIX: str = "yt"
_SHORT_HOST: str = "youtu.be"
_WATCH_HOST_SUFFIX: str = "youtube.com"
_VIDEO_ID_PARAM: str = "v"

_ARTIFACTS_FILENAME: str = "artifacts.json"
_TRANSCRIPT_FILENAME: str = "transcript.json"
_KEYFRAMES_DIRNAME: str = "keyframes"
_KEYFRAME_INDEX_FILENAME: str = "index.json"
_ON_SCREEN_FILENAME: str = "on_screen.json"

_JSON_INDENT: int = 2
_ENCODING: str = "utf-8"


class IngestionError(Exception):
    """Raised when a video cannot be ingested because a required input is absent."""


@dataclass(frozen=True)
class IngestionSeams:
    """The injected boundary implementations for each deterministic ingestion stage."""

    downloader: Downloader
    transcriber: Transcriber
    detector: SceneDetector
    extractor: FrameExtractor
    on_screen: OnScreenExtractor


def production_seams(config: Config) -> IngestionSeams:  # pragma: no cover
    """Build the real, expensive ingestion seams (yt-dlp, faster-whisper, scenedetect, OpenCV, VLM)."""
    return IngestionSeams(
        downloader=make_ytdlp_downloader(),
        transcriber=make_faster_whisper_transcriber(config),
        detector=make_scenedetect_detector(config),
        extractor=make_opencv_extractor(),
        on_screen=make_on_screen_extractor(config),
    )


def _source_id_from_url(url: str) -> str:
    """Extract the YouTube video id from a watch or short URL and return it as a `yt:<id>` source id."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == _SHORT_HOST or host.endswith(f".{_SHORT_HOST}"):
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif host == _WATCH_HOST_SUFFIX or host.endswith(f".{_WATCH_HOST_SUFFIX}"):
        video_id = next(iter(parse_qs(parsed.query).get(_VIDEO_ID_PARAM, [])), "")
    else:
        video_id = ""
    if not video_id:
        raise ValueError(f"No recognizable YouTube video id in URL: {url!r}")
    return f"{_SOURCE_ID_PREFIX}:{video_id}"


def _cached_model(path: Path, model_cls: type[T], compute: Callable[[], T]) -> T:
    """Return a cached pydantic model from `path` if present, otherwise compute, persist, and return it."""
    if path.exists():
        return model_cls.model_validate_json(path.read_text(encoding=_ENCODING))
    result = compute()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=_JSON_INDENT), encoding=_ENCODING)
    return result


def _cached_list(path: Path, item_cls: type[M], compute: Callable[[], list[M]]) -> list[M]:
    """Return a cached list of pydantic models from `path` if present, otherwise compute, persist, and return it."""
    adapter: TypeAdapter[list[M]] = TypeAdapter(list[item_cls])
    if path.exists():
        return adapter.validate_json(path.read_text(encoding=_ENCODING))
    result = compute()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(adapter.dump_json(result, indent=_JSON_INDENT).decode(_ENCODING), encoding=_ENCODING)
    return result


def _resolve_transcript(artifacts: VideoArtifacts, seams: IngestionSeams) -> TranscriptResult:
    """Prefer a caption-derived transcript, fall back to audio transcription, and fail loudly if neither is available."""
    caption_transcript = select_caption_transcript(artifacts)
    if caption_transcript is not None:
        return caption_transcript
    if artifacts.audio_path is None:
        raise IngestionError(
            f"No captions and no audio track available for {artifacts.source_ref.source_id}; cannot transcribe."
        )
    return seams.transcriber(artifacts.audio_path)


def _resolve_keyframes(
    artifacts: VideoArtifacts,
    source_dir: Path,
    seams: IngestionSeams,
    max_frames: int,
) -> list[Keyframe]:
    """Extract keyframes from the fetched video, or return an empty list when no video track was downloaded."""
    if artifacts.video_path is None:
        return []
    return select_keyframes(
        artifacts.video_path,
        source_dir / _KEYFRAMES_DIRNAME,
        seams.detector,
        seams.extractor,
        max_frames=max_frames,
    )


def ingest_video(
    url: str,
    slug: str,
    cache_dir: Path,
    seams: IngestionSeams,
    *,
    max_frames: int,
) -> VideoPayload:
    """Orchestrate the deterministic ingestion stages into a VideoPayload, caching each stage's product on disk.

    Each expensive stage (download, transcription, keyframing, VLM) is skipped on a re-run whenever its
    cache artifact already exists, extending the ADR-0002 recoverability ladder to a per-stage granularity.
    """
    source_id = _source_id_from_url(url)
    source_dir = cache_dir / source_id_to_dirname(source_id)
    source_dir.mkdir(parents=True, exist_ok=True)

    artifacts = _cached_model(
        source_dir / _ARTIFACTS_FILENAME,
        VideoArtifacts,
        lambda: seams.downloader(url, source_dir),
    )
    transcript = _cached_model(
        source_dir / _TRANSCRIPT_FILENAME,
        TranscriptResult,
        lambda: _resolve_transcript(artifacts, seams),
    )
    keyframes = _cached_list(
        source_dir / _KEYFRAMES_DIRNAME / _KEYFRAME_INDEX_FILENAME,
        Keyframe,
        lambda: _resolve_keyframes(artifacts, source_dir, seams, max_frames),
    )
    extractions = _cached_list(
        source_dir / _ON_SCREEN_FILENAME,
        OnScreenExtraction,
        lambda: seams.on_screen(keyframes),
    )
    return build_video_payload(slug, artifacts.source_ref, transcript, extractions)
