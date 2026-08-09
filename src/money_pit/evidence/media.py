"""Module containing timestamped audio and video evidence processing."""

# Optional native media libraries do not publish complete typing metadata; this
# module validates and converts every value at their boundary.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Protocol

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic_ai import Agent
from pydantic_ai import BinaryContent

from money_pit.agents.models import openai_chat_model
from money_pit.evidence.results import DerivedEvidenceDocument
from money_pit.evidence.results import EvidenceProcessingBundle
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TimestampLocator
from money_pit.sources._shared import evidence_asset
from money_pit.sources.errors import SourceExtractionError


if TYPE_CHECKING:
    from money_pit.config import Config
    from money_pit.schemas.sources import RawArtifact


class TimedTranscriptSegment(BaseModel):
    """One timestamped transcript segment."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str = Field(min_length=1)


class FrameReading(BaseModel):
    """Visible financial evidence extracted from one keyframe."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    timestamp_seconds: float = Field(ge=0)
    on_screen_text: tuple[str, ...] = ()
    cited_sources: tuple[str, ...] = ()
    bounding_box: tuple[float, float, float, float] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    extraction_model: str | None = None
    image_png: bytes


class MediaAnalysis(BaseModel):
    """Complete transcript and frame analysis for one media acquisition."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    transcript: tuple[TimedTranscriptSegment, ...]
    frames: tuple[FrameReading, ...] = ()


class MediaAnalyzer(Protocol):
    """Expensive media-analysis seam used by the evidence processor."""

    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        """Return timestamped transcript and visible frame evidence."""
        ...


class _VisionDraft(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    on_screen_text: tuple[str, ...] = ()
    cited_sources: tuple[str, ...] = ()
    bounding_box: tuple[float, float, float, float] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class OpenAIVisionFrameReader:
    """Read keyframe text, source labels, and evidence bounds with a vision model."""

    def __init__(self, config: Config) -> None:
        """Bind the configured image-capable model without resolving it eagerly."""
        self._config: Config = config
        self._model: str = config.llm_model
        self._agent: Agent[None, _VisionDraft] | None = None

    def _inference_agent(self) -> Agent[None, _VisionDraft]:
        if self._agent is not None:
            return self._agent
        self._agent = Agent(
            openai_chat_model(self._config),
            output_type=_VisionDraft,
            system_prompt=(
                "Extract visible financial text and explicit source attribution from the image. "
                "Do not infer text that is not visible. Return a normalized bounding box when the "
                "relevant content occupies a bounded region."
            ),
        )
        return self._agent

    def read(self, image: bytes, *, timestamp_seconds: float) -> FrameReading:
        """Read one PNG keyframe into typed evidence."""
        try:
            result = self._inference_agent().run_sync(
                [
                    "Extract the on-screen text and cited source labels.",
                    BinaryContent(data=image, media_type="image/png"),
                ],
            )
        except Exception as error:
            raise SourceExtractionError("Video frame interpretation failed") from error
        draft: _VisionDraft = result.output
        return FrameReading(
            timestamp_seconds=timestamp_seconds,
            on_screen_text=draft.on_screen_text,
            cited_sources=draft.cited_sources,
            bounding_box=draft.bounding_box,
            confidence=draft.confidence,
            extraction_model=self._model,
            image_png=image,
        )


class DefaultMediaAnalyzer:
    """Analyze media with faster-whisper, scene detection, OpenCV, and vision."""

    def __init__(
        self,
        *,
        whisper_model: str = "small",
        maximum_frames: int = 24,
        scene_threshold: float = 27.0,
        frame_reader: OpenAIVisionFrameReader,
    ) -> None:
        """Bind explicit transcription and frame-analysis limits."""
        if maximum_frames < 0:
            raise ValueError("maximum_frames must not be negative")
        self._whisper_model: str = whisper_model
        self._maximum_frames: int = maximum_frames
        self._scene_threshold: float = scene_threshold
        self._frame_reader: OpenAIVisionFrameReader = frame_reader

    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        """Run timestamped transcription and bounded video-frame analysis."""
        transcript: tuple[TimedTranscriptSegment, ...] = self._transcribe(path)
        frames: tuple[FrameReading, ...] = ()
        if media_type.partition(";")[0].casefold().startswith("video/"):
            frames = self._read_frames(path)
        return MediaAnalysis(transcript=transcript, frames=frames)

    def _transcribe(self, path: Path) -> tuple[TimedTranscriptSegment, ...]:
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(self._whisper_model, device="auto", compute_type="int8")
            segment_stream, _ = model.transcribe(str(path), word_timestamps=True)
            return tuple(
                TimedTranscriptSegment(
                    start_seconds=float(segment.start),
                    end_seconds=float(segment.end),
                    text=text,
                )
                for segment in segment_stream
                if (text := str(segment.text).strip())
            )
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            raise SourceExtractionError("Media transcription failed") from error

    def _read_frames(self, path: Path) -> tuple[FrameReading, ...]:
        try:
            import cv2
            from scenedetect import ContentDetector
            from scenedetect import detect

            scenes = detect(str(path), ContentDetector(threshold=self._scene_threshold))
            timestamps: list[float] = [float(start.get_seconds()) for start, _ in scenes]
            selected: list[float] = _evenly_capped(timestamps, self._maximum_frames)
            capture = cv2.VideoCapture(str(path))
            readings: list[FrameReading] = []
            try:
                for timestamp in selected:
                    _ = capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
                    success, frame = capture.read()
                    if not success:
                        continue
                    encoded, png = cv2.imencode(".png", frame)
                    if encoded:
                        readings.append(
                            self._frame_reader.read(bytes(png), timestamp_seconds=timestamp),
                        )
            finally:
                capture.release()
            return tuple(readings)
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            raise SourceExtractionError("Video frame analysis failed") from error


class MediaEvidenceProcessor:
    """Convert audio or video into timestamped transcript and frame fragments."""

    name: str = "timestamped-media"
    version: str = "1"

    def __init__(self, analyzer: MediaAnalyzer) -> None:
        """Bind an explicit analyzer so credentials never resolve ambiently."""
        self._analyzer: MediaAnalyzer = analyzer

    def supports(self, media_type: str) -> bool:
        """Accept general audio and video acquisitions."""
        normalized: str = media_type.partition(";")[0].strip().casefold()
        return normalized.startswith("audio/") or normalized.startswith("video/")

    def process(self, acquisition: RawArtifact) -> EvidenceDocument:
        """Analyze one immutable media acquisition in an isolated temporary directory."""
        return self.process_bundle(acquisition).primary

    def process_bundle(self, acquisition: RawArtifact) -> EvidenceProcessingBundle:
        """Return primary transcript evidence and immutable derived frame assets."""
        suffix: str = acquisition.filename.suffix if acquisition.filename is not None else ".media"
        try:
            with tempfile.TemporaryDirectory(prefix="money-pit-media-") as temporary:
                media_path: Path = Path(temporary, f"acquisition{suffix}")
                _ = media_path.write_bytes(acquisition.content)
                analysis: MediaAnalysis = self._analyzer.analyze(
                    media_path,
                    media_type=acquisition.media_type,
                )
        except OSError as error:
            raise SourceExtractionError("Media acquisition could not be staged") from error
        fragments: list[EvidenceFragment] = []
        for index, segment in enumerate(analysis.transcript):
            fragments.append(
                EvidenceFragment(
                    fragment_id=_fragment_id(acquisition.content_hash, "transcript", index, segment.text),
                    asset_id=acquisition.content_hash,
                    kind="transcript",
                    locator=TimestampLocator(
                        start_seconds=segment.start_seconds,
                        end_seconds=segment.end_seconds,
                    ),
                    extracted_text=segment.text,
                    extraction_method=self.name,
                    extraction_model=getattr(self._analyzer, "_whisper_model", None),
                ),
            )
        derived: list[DerivedEvidenceDocument] = []
        for frame_index, frame in enumerate(analysis.frames):
            frame_asset_id: str = hashlib.sha256(frame.image_png).hexdigest()
            entries: tuple[tuple[str, str], ...] = tuple(("text", value) for value in frame.on_screen_text) + tuple(
                ("source", value) for value in frame.cited_sources
            )
            frame_fragments: list[EvidenceFragment] = []
            for entry_index, (entry_kind, value) in enumerate(entries):
                frame_fragments.append(
                    EvidenceFragment(
                        fragment_id=_fragment_id(
                            frame_asset_id,
                            f"parent:{acquisition.content_hash}:frame:{frame_index}:{entry_kind}",
                            entry_index,
                            value,
                        ),
                        asset_id=frame_asset_id,
                        kind="frame",
                        locator=TimestampLocator(
                            start_seconds=frame.timestamp_seconds,
                            bounding_box=frame.bounding_box,
                        ),
                        extracted_text=value if entry_kind == "text" else None,
                        cited_source_text=value if entry_kind == "source" else None,
                        extraction_method="vision_llm",
                        extraction_model=frame.extraction_model,
                        confidence=frame.confidence,
                    )
                )
            derived.append(
                DerivedEvidenceDocument(
                    content=frame.image_png,
                    document=EvidenceDocument(
                        asset=evidence_asset(acquisition).model_copy(
                            update={
                                "asset_id": frame_asset_id,
                                "content_hash": frame_asset_id,
                                "media_type": "image/png",
                                "local_path": Path(frame_asset_id[:2], frame_asset_id),
                            },
                        ),
                        fragments=tuple(frame_fragments),
                    ),
                ),
            )
        return EvidenceProcessingBundle(
            primary=EvidenceDocument(asset=evidence_asset(acquisition), fragments=tuple(fragments)),
            derived=tuple(derived),
        )


def _evenly_capped(timestamps: list[float], maximum: int) -> list[float]:
    if maximum <= 0:
        return []
    if len(timestamps) <= maximum:
        return timestamps
    if maximum == 1:
        return timestamps[:1]
    stride: float = (len(timestamps) - 1) / (maximum - 1)
    return [timestamps[round(index * stride)] for index in range(maximum)]


def _fragment_id(asset_id: str, kind: str, index: int, text: str) -> str:
    digest: str = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{asset_id}:{kind}:{index:06d}:{digest}"
