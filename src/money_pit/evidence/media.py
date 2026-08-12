"""Module containing timestamped audio and video evidence processing."""

# Optional native media libraries do not publish complete typing metadata; this
# module validates and converts every value at their boundary.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false

from __future__ import annotations

import hashlib
import math
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Annotated
from typing import ClassVar
from typing import Protocol
from typing import cast

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic_ai import Agent
from pydantic_ai import BinaryContent

from money_pit.agents.models import openai_responses_model
from money_pit.evidence.results import DerivedEvidenceDocument
from money_pit.evidence.results import EvidenceProcessingBundle
from money_pit.progress import IngestionProgressCallback
from money_pit.progress import IngestionProgressEvent
from money_pit.progress import IngestionProgressStage
from money_pit.progress import ignore_ingestion_progress
from money_pit.prompt_loader import system_prompt
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TimestampLocator
from money_pit.sources._shared import evidence_asset
from money_pit.sources.errors import SourceExtractionError


if TYPE_CHECKING:
    from collections.abc import Callable

    from scenedetect.detector import SceneDetector

    from money_pit.schemas.sources import RawArtifact
    from money_pit.secrets import OpenAICredentials


class TimedTranscriptSegment(BaseModel):
    """One timestamped transcript segment."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str = Field(min_length=1)


def _validate_normalized_bounding_box(
    bounding_box: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    left, top, right, bottom = bounding_box
    if left > right or top > bottom:
        raise ValueError("bounding_box coordinates must define valid geometry.")
    return bounding_box


_NormalizedCoordinate = Annotated[float, Field(ge=0, le=1)]
_NormalizedBoundingBox = Annotated[
    tuple[
        _NormalizedCoordinate,
        _NormalizedCoordinate,
        _NormalizedCoordinate,
        _NormalizedCoordinate,
    ],
    AfterValidator(_validate_normalized_bounding_box),
]


class FrameReading(BaseModel):
    """Visible financial evidence extracted from one keyframe."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    timestamp_seconds: float = Field(ge=0)
    on_screen_text: tuple[str, ...] = ()
    cited_sources: tuple[str, ...] = ()
    bounding_box: _NormalizedBoundingBox | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    extraction_model: str | None = None
    image_png: bytes


class MediaAnalysis(BaseModel):
    """Complete transcript and frame analysis for one media acquisition."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    transcript: tuple[TimedTranscriptSegment, ...]
    frames: tuple[FrameReading, ...] = ()


class TranscriptionBackend(BaseModel):
    """One validated faster-whisper execution backend."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    device: str = Field(pattern=r"^(cpu|cuda)$")
    compute_type: str = Field(pattern=r"^(int8|float16)$")


class CudaDeviceCounter(Protocol):
    """Return the number of CUDA devices visible to CTranslate2."""

    def __call__(self) -> int:
        """Return the visible CUDA device count."""
        ...


class ComputeTypeProvider(Protocol):
    """Return CTranslate2 compute types supported by one device class."""

    def __call__(self, device: str, /) -> set[str]:
        """Return supported compute-type names."""
        ...


def _default_cuda_device_count() -> int:
    from ctranslate2 import get_cuda_device_count  # pyright: ignore[reportUnknownVariableType]

    return int(get_cuda_device_count())  # pyright: ignore[reportUnknownArgumentType]


def _default_supported_compute_types(device: str, /) -> set[str]:
    from ctranslate2 import get_supported_compute_types  # pyright: ignore[reportUnknownVariableType]

    values: object = cast("object", get_supported_compute_types(device))
    if not isinstance(values, (list, set, tuple)):
        return set()
    validated_values = cast("list[object] | set[object] | tuple[object, ...]", values)
    return {value for value in validated_values if isinstance(value, str)}


def select_transcription_backend(
    *,
    cuda_device_count: CudaDeviceCounter | None = None,
    supported_compute_types: ComputeTypeProvider | None = None,
) -> TranscriptionBackend:
    """Prefer CUDA float16 only when CTranslate2 reports complete support."""
    count_provider: CudaDeviceCounter = cuda_device_count or _default_cuda_device_count
    compute_type_provider: ComputeTypeProvider = supported_compute_types or _default_supported_compute_types
    try:
        if count_provider() > 0 and "float16" in compute_type_provider("cuda"):
            return TranscriptionBackend(device="cuda", compute_type="float16")
    except (ImportError, OSError, RuntimeError, ValueError):
        pass
    return TranscriptionBackend(device="cpu", compute_type="int8")


class MediaAnalyzer(Protocol):
    """Expensive media-analysis seam used by the evidence processor."""

    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        """Return timestamped transcript and visible frame evidence."""
        ...


class _VisionDraft(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    on_screen_text: tuple[str, ...] = ()
    cited_sources: tuple[str, ...] = ()
    bounding_box: _NormalizedBoundingBox | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class OpenAIVisionFrameReader:
    """Read keyframe text, source labels, and evidence bounds with a vision model."""

    def __init__(self, load_credentials: Callable[[], OpenAICredentials], model: str) -> None:
        """Bind the configured image-capable model without resolving it eagerly."""
        self._load_credentials: Callable[[], OpenAICredentials] = load_credentials
        self._model: str = model
        self._agent: Agent[None, _VisionDraft] | None = None

    def _inference_agent(self) -> Agent[None, _VisionDraft]:
        if self._agent is not None:
            return self._agent
        self._agent = Agent(
            openai_responses_model(
                self._load_credentials(),
                model_name=self._model,
            ),
            output_type=_VisionDraft,
            system_prompt=system_prompt("agent_video_onscreen"),
        )
        return self._agent

    def read(self, image: bytes, *, timestamp_seconds: float) -> FrameReading:
        """Read one PNG keyframe into typed evidence."""
        try:
            result = self._inference_agent().run_sync(
                [
                    "Analyze this keyframe according to the system prompt and return all applicable structured fields.",
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


class _ContentDetectorDelegate(Protocol):
    """PySceneDetect methods used by the progress-reporting delegate."""

    stats_manager: object | None

    @property
    def event_buffer_length(self) -> int:
        """Return the event look-behind requirement."""
        ...

    def get_metrics(self) -> list[str]:
        """Return statistics metric names."""
        ...

    def process_frame(self, timecode: object, frame_image: object) -> list[object]:
        """Process one probed frame."""
        ...

    def post_process(self, timecode: object) -> list[object]:
        """Finish detector processing."""
        ...


class _ProgressContentDetector:
    """Delegate scene detection while emitting throttled probe completion."""

    def __init__(
        self,
        detector: object,
        progress: IngestionProgressCallback,
        *,
        expected_probes: int,
    ) -> None:
        self._detector: _ContentDetectorDelegate = cast("_ContentDetectorDelegate", detector)
        self._progress: IngestionProgressCallback = progress
        self._expected_probes: int = max(expected_probes, 1)
        self._processed: int = 0
        self._last_percentage: int = 0
        self._stats_manager: object | None = None
        self._emit(0)

    @property
    def stats_manager(self) -> object | None:
        """Return the delegate's PySceneDetect statistics manager."""
        return self._stats_manager

    @stats_manager.setter
    def stats_manager(self, value: object | None) -> None:
        self._stats_manager = value
        self._detector.stats_manager = value

    @property
    def event_buffer_length(self) -> int:
        """Return the delegate's event look-behind requirement."""
        return self._detector.event_buffer_length

    def get_metrics(self) -> list[str]:
        """Return the delegate's optional statistics metrics."""
        return self._detector.get_metrics()

    def process_frame(self, timecode: object, frame_image: object) -> list[object]:
        """Process one probed frame and report bounded completion."""
        result: list[object] = self._detector.process_frame(timecode, frame_image)
        self._processed += 1
        percentage: int = min(int(self._processed / self._expected_probes * 100), 100)
        if percentage > self._last_percentage:
            self._emit(percentage)
        return result

    def post_process(self, timecode: object) -> list[object]:
        """Forward end-of-stream processing to the delegate."""
        return self._detector.post_process(timecode)

    def complete(self) -> None:
        """Report completion after SceneManager joins its decode loop."""
        if self._last_percentage < 100:
            self._emit(100)

    def _emit(self, percentage: int) -> None:
        self._last_percentage = percentage
        self._progress(
            IngestionProgressEvent(
                stage=IngestionProgressStage.SCENE_DETECTION,
                current=float(percentage),
                total=100.0,
            ),
        )


class _TranscriptionProgressReporter:
    """Emit at most one transcription event for each completed percentage."""

    def __init__(
        self,
        progress: IngestionProgressCallback,
        *,
        duration_seconds: float | None,
    ) -> None:
        self._progress: IngestionProgressCallback = progress
        self._duration_seconds: float | None = duration_seconds
        self._last_percentage: int = -1

    def advance(self, end_seconds: float) -> None:
        """Report a new integer percentage when duration metadata is available."""
        if self._duration_seconds is None:
            return
        percentage: int = min(int(end_seconds / self._duration_seconds * 100), 99)
        if percentage > self._last_percentage:
            self._emit(percentage)

    def complete(self) -> None:
        """Report successful completion after the segment iterator is exhausted."""
        if self._last_percentage < 100:
            self._emit(100)

    def _emit(self, percentage: int) -> None:
        self._last_percentage = percentage
        self._progress(
            IngestionProgressEvent(
                stage=IngestionProgressStage.TRANSCRIPTION_PROGRESS,
                current=float(percentage),
                total=100.0,
            ),
        )


class DefaultMediaAnalyzer:
    """Analyze media with faster-whisper, scene detection, OpenCV, and vision."""

    def __init__(
        self,
        *,
        whisper_model: str = "small",
        maximum_frames: int = 24,
        maximum_scene_probe_frames: int = 2_000,
        scene_threshold: float = 27.0,
        frame_reader: OpenAIVisionFrameReader,
        progress: IngestionProgressCallback = ignore_ingestion_progress,
        transcription_backend_selector: Callable[[], TranscriptionBackend] = select_transcription_backend,
    ) -> None:
        """Bind explicit transcription and frame-analysis limits."""
        if maximum_frames < 0:
            raise ValueError("maximum_frames must not be negative")
        if maximum_scene_probe_frames <= 0:
            raise ValueError("maximum_scene_probe_frames must be positive")
        self._whisper_model: str = whisper_model
        self._maximum_frames: int = maximum_frames
        self._maximum_scene_probe_frames: int = maximum_scene_probe_frames
        self._scene_threshold: float = scene_threshold
        self._frame_reader: OpenAIVisionFrameReader = frame_reader
        self._progress: IngestionProgressCallback = progress
        self._transcription_backend_selector: Callable[[], TranscriptionBackend] = transcription_backend_selector

    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        """Run timestamped transcription and bounded video-frame analysis."""
        transcript: tuple[TimedTranscriptSegment, ...] = self._transcribe(path)
        frames: tuple[FrameReading, ...] = ()
        if media_type.partition(";")[0].casefold().startswith("video/"):
            frames = self._read_frames(path)
        return MediaAnalysis(transcript=transcript, frames=frames)

    def _transcribe(self, path: Path) -> tuple[TimedTranscriptSegment, ...]:
        try:
            backend: TranscriptionBackend = self._transcription_backend_selector()
            self._emit_backend(backend, IngestionProgressStage.TRANSCRIPTION_BACKEND)
            try:
                return self._transcribe_with_backend(path, backend)
            except (OSError, RuntimeError, ValueError) as error:
                if backend.device != "cuda" or not _is_cuda_capability_error(error):
                    raise
                fallback = TranscriptionBackend(device="cpu", compute_type="int8")
                self._emit_backend(fallback, IngestionProgressStage.TRANSCRIPTION_FALLBACK)
                return self._transcribe_with_backend(path, fallback)
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            raise SourceExtractionError("Media transcription failed") from error

    def _transcribe_with_backend(
        self,
        path: Path,
        backend: TranscriptionBackend,
    ) -> tuple[TimedTranscriptSegment, ...]:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            self._whisper_model,
            device=backend.device,
            compute_type=backend.compute_type,
        )
        segment_stream, info = model.transcribe(str(path), word_timestamps=True)
        duration: float | None = _positive_float(getattr(info, "duration", None))
        progress = _TranscriptionProgressReporter(
            self._progress,
            duration_seconds=duration,
        )
        transcript: list[TimedTranscriptSegment] = []
        for segment in segment_stream:
            text: str = str(segment.text).strip()
            end_seconds: float = float(segment.end)
            if text:
                transcript.append(
                    TimedTranscriptSegment(
                        start_seconds=float(segment.start),
                        end_seconds=end_seconds,
                        text=text,
                    ),
                )
            progress.advance(end_seconds)
        progress.complete()
        return tuple(transcript)

    def _emit_backend(
        self,
        backend: TranscriptionBackend,
        stage: IngestionProgressStage,
    ) -> None:
        self._progress(
            IngestionProgressEvent(
                stage=stage,
                detail=f"{backend.device}/{backend.compute_type}",
            ),
        )

    def _read_frames(self, path: Path) -> tuple[FrameReading, ...]:
        try:
            import cv2
            from scenedetect import ContentDetector
            from scenedetect import SceneManager
            from scenedetect import open_video  # pyright: ignore[reportUnknownVariableType]

            video = open_video(str(path))
            duration = video.duration
            if duration is None:
                raise ValueError("Video duration is unavailable")
            total_frames: int = int(duration.frame_num)
            duration_seconds: float = float(duration.get_seconds())
            frame_skip: int = _scene_probe_frame_skip(total_frames, self._maximum_scene_probe_frames)
            expected_probes: int = _expected_scene_probes(total_frames, frame_skip)
            detector = _ProgressContentDetector(
                ContentDetector(threshold=self._scene_threshold),
                self._progress,
                expected_probes=expected_probes,
            )
            manager = SceneManager()
            manager.add_detector(cast("SceneDetector", cast("object", detector)))
            _ = manager.detect_scenes(video=video, frame_skip=frame_skip, show_progress=False)
            detector.complete()
            cut_timestamps: list[float] = [
                float(timecode.get_seconds()) for timecode in manager.get_cut_list(show_warning=False)
            ]
            selected: list[float] = _hybrid_frame_timestamps(
                duration_seconds,
                cut_timestamps,
                min(self._maximum_frames, total_frames),
            )
            capture = cv2.VideoCapture(str(path))
            readings: list[FrameReading] = []
            try:
                for index, timestamp in enumerate(selected, start=1):
                    try:
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
                        self._progress(
                            IngestionProgressEvent(
                                stage=IngestionProgressStage.FRAME_PROCESSING,
                                current=float(index),
                                total=float(len(selected)),
                            ),
                        )
            finally:
                capture.release()
            return tuple(readings)
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            raise SourceExtractionError("Video frame analysis failed") from error


class MediaEvidenceProcessor:
    """Convert audio or video into timestamped transcript and frame fragments."""

    name: str = "timestamped-media"
    version: str = "2"

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


def _scene_probe_frame_skip(total_frames: int, maximum_probes: int) -> int:
    """Return a PySceneDetect skip that decodes no more than the probe budget."""
    if total_frames < 0:
        raise ValueError("total_frames must not be negative")
    if maximum_probes <= 0:
        raise ValueError("maximum_probes must be positive")
    return max(math.ceil(total_frames / maximum_probes) - 1, 0)


def _expected_scene_probes(total_frames: int, frame_skip: int) -> int:
    """Return the bounded number of frames SceneManager will probe."""
    if total_frames < 0 or frame_skip < 0:
        raise ValueError("frame counts and skip must not be negative")
    if total_frames == 0:
        return 1
    return math.ceil(total_frames / (frame_skip + 1))


def _hybrid_frame_timestamps(
    duration_seconds: float,
    cut_timestamps: list[float],
    maximum_frames: int,
) -> list[float]:
    """Cover the full timeline, preferring one detected cut in each time bin."""
    if duration_seconds < 0:
        raise ValueError("duration_seconds must not be negative")
    if maximum_frames < 0:
        raise ValueError("maximum_frames must not be negative")
    if duration_seconds == 0 or maximum_frames == 0:
        return []
    bin_count: int = maximum_frames
    bin_width: float = duration_seconds / bin_count
    usable_cuts: list[float] = sorted(timestamp for timestamp in cut_timestamps if 0 <= timestamp < duration_seconds)
    selected: list[float] = []
    for index in range(bin_count):
        start: float = index * bin_width
        end: float = duration_seconds if index == bin_count - 1 else (index + 1) * bin_width
        midpoint: float = start + (end - start) / 2
        cuts_in_bin: list[float] = [timestamp for timestamp in usable_cuts if start <= timestamp < end]
        selected.append(
            min(cuts_in_bin, key=lambda timestamp: abs(timestamp - midpoint)) if cuts_in_bin else midpoint,
        )
    return selected


def _is_cuda_capability_error(error: Exception) -> bool:
    """Return whether CTranslate2 identified a CUDA capability failure."""
    message: str = str(error).casefold()
    markers: tuple[str, ...] = (
        "cuda",
        "cublas",
        "cudnn",
        "compute type",
        "gpu driver",
    )
    return any(marker in message for marker in markers)


def _positive_float(value: object) -> float | None:
    try:
        converted: float = float(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return None
    return converted if converted > 0 else None


def _fragment_id(asset_id: str, kind: str, index: int, text: str) -> str:
    digest: str = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{asset_id}:{kind}:{index:06d}:{digest}"
