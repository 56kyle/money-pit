import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from typing import cast

import pytest
from pytest import MonkeyPatch

import money_pit.evidence.media as media
from money_pit.evidence.media import DefaultMediaAnalyzer
from money_pit.evidence.media import FrameReading
from money_pit.evidence.media import MediaAnalysis
from money_pit.evidence.media import OpenAIVisionFrameReader
from money_pit.evidence.media import TranscriptionBackend
from money_pit.evidence.media import select_transcription_backend
from money_pit.progress import IngestionProgressEvent
from money_pit.progress import IngestionProgressStage
from money_pit.sources.errors import SourceExtractionError


class _UnusedMediaAnalyzer:
    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        raise AssertionError((path, media_type))


class _WhisperModel:
    calls: ClassVar[list[tuple[str, str]]] = []
    cuda_failure: ClassVar[str | None] = None

    def __init__(self, model: str, *, device: str, compute_type: str) -> None:
        del model
        self._device: str = device
        self.__class__.calls.append((device, compute_type))

    def transcribe(self, path: str, *, word_timestamps: bool) -> tuple[tuple[object, ...], SimpleNamespace]:
        del path, word_timestamps
        if self._device == "cuda" and self.cuda_failure is not None:
            raise RuntimeError(self.cuda_failure)
        return (), SimpleNamespace(duration=1.0)
class _FrameReader:
    def __init__(self, events: list[IngestionProgressEvent]) -> None:
        self._events: list[IngestionProgressEvent] = events
        self.progress_at_read: list[tuple[float, ...]] = []

    def read(self, image: bytes, *, timestamp_seconds: float) -> FrameReading:
        self.progress_at_read.append(
            tuple(
                event.current
                for event in self._events
                if event.stage == IngestionProgressStage.FRAME_PROCESSING and event.current is not None
            ),
        )
        return FrameReading(timestamp_seconds=timestamp_seconds, image_png=image)


class _Capture:
    def set(self, property_id: int, value: float) -> bool:
        del property_id, value
        return True

    def read(self) -> tuple[bool, bytes]:
        return True, b"frame"

    def release(self) -> None:
        return None


class _Timestamp:
    frame_num: ClassVar[int] = 4_001

    def __init__(self, seconds: float) -> None:
        self._seconds: float = seconds

    def get_seconds(self) -> float:
        return self._seconds


class _Video:
    duration: ClassVar[_Timestamp] = _Timestamp(4)


class _SceneDetector:
    event_buffer_length: int = 0
    stats_manager: object | None = None

    def get_metrics(self) -> list[str]:
        return []

    def process_frame(self, timecode: object, frame_image: object) -> tuple[object, object]:
        return timecode, frame_image

    def post_process(self, timecode: object) -> object:
        return timecode


class _SceneManager:
    detector: object | None = None

    def add_detector(self, detector: object) -> None:
        self.detector = detector

    def detect_scenes(self, *, video: object, frame_skip: int, show_progress: bool) -> int:
        del video, frame_skip, show_progress
        return 0

    def get_cut_list(self, *, show_warning: bool) -> tuple[object, ...]:
        del show_warning
        return ()


def _video_capture(path: str) -> _Capture:
    del path
    return _Capture()


def _imencode(extension: str, frame: object) -> tuple[bool, bytearray]:
    del extension, frame
    return True, bytearray(b"png")


def _content_detector(*, threshold: float) -> _SceneDetector:
    del threshold
    return _SceneDetector()


def _open_video(path: str) -> _Video:
    del path
    return _Video()


def _analyzer(
    events: list[IngestionProgressEvent],
) -> DefaultMediaAnalyzer:
    return DefaultMediaAnalyzer(
        frame_reader=cast("OpenAIVisionFrameReader", cast("object", _UnusedMediaAnalyzer())),
        progress=events.append,
        transcription_backend_selector=lambda: TranscriptionBackend(device="cuda", compute_type="float16"),
    )


def test_select_transcription_backend_with_supported_cuda_float16() -> None:
    backend = select_transcription_backend(
        cuda_device_count=lambda: 1,
        supported_compute_types=lambda device: {"float16"} if device == "cuda" else set(),
    )

    assert backend == TranscriptionBackend(device="cuda", compute_type="float16")


@pytest.mark.parametrize(
    ("device_count", "compute_types"),
    [
        pytest.param(0, {"float16"}, id="no-cuda-device"),
        pytest.param(1, {"int8"}, id="no-cuda-float16"),
    ],
)
def test_select_transcription_backend_without_complete_cuda_support(
    device_count: int,
    compute_types: set[str],
) -> None:
    backend = select_transcription_backend(
        cuda_device_count=lambda: device_count,
        supported_compute_types=lambda _device: compute_types,
    )

    assert backend == TranscriptionBackend(device="cpu", compute_type="int8")


def test__transcribe_falls_back_to_cpu_int8_only_for_a_cuda_capability_failure(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[IngestionProgressEvent] = []
    _WhisperModel.calls = []
    _WhisperModel.cuda_failure = "cuBLAS is unavailable for this CUDA device"
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=_WhisperModel))

    transcript = _analyzer(events)._transcribe(tmp_path / "media.mp4")  # pyright: ignore[reportPrivateUsage]

    assert transcript == ()
    assert _WhisperModel.calls == [("cuda", "float16"), ("cpu", "int8")]
    assert tuple(event.stage for event in events) == (
        IngestionProgressStage.TRANSCRIPTION_BACKEND,
        IngestionProgressStage.TRANSCRIPTION_FALLBACK,
        IngestionProgressStage.TRANSCRIPTION_PROGRESS,
    )
    assert events[-1].current == 100


def test__transcription_progress_reporter_emits_distinct_monotonic_percentages_and_completion() -> None:
    events: list[IngestionProgressEvent] = []
    reporter = media._TranscriptionProgressReporter(  # pyright: ignore[reportPrivateUsage]
        events.append,
        duration_seconds=100,
    )

    for end_seconds in (1.01, 1.1, 1.99, 2, 2.1, 2.99, 50, 99, 100):
        reporter.advance(end_seconds)
    reporter.complete()

    percentages = tuple(event.current for event in events if event.current is not None)
    assert percentages == (1.0, 2.0, 50.0, 99.0, 100.0)
    assert percentages == tuple(sorted(set(percentages)))


def test__transcription_progress_reporter_with_unknown_duration_emits_only_success_completion() -> None:
    events: list[IngestionProgressEvent] = []
    reporter = media._TranscriptionProgressReporter(  # pyright: ignore[reportPrivateUsage]
        events.append,
        duration_seconds=None,
    )

    reporter.advance(1_000)
    reporter.complete()

    assert tuple(event.current for event in events) == (100.0,)


def test__transcribe_does_not_hide_an_unrelated_cuda_runtime_failure(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[IngestionProgressEvent] = []
    _WhisperModel.calls = []
    _WhisperModel.cuda_failure = "media decoder returned malformed timestamps"
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=_WhisperModel))

    with pytest.raises(SourceExtractionError):
        _ = _analyzer(events)._transcribe(tmp_path / "media.mp4")  # pyright: ignore[reportPrivateUsage]

    assert _WhisperModel.calls == [("cuda", "float16")]


def test__read_frames_emits_each_completion_after_its_frame_inference(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[IngestionProgressEvent] = []
    reader = _FrameReader(events)
    cv2 = SimpleNamespace(
        CAP_PROP_POS_MSEC=0,
        VideoCapture=_video_capture,
        imencode=_imencode,
    )
    scenedetect = SimpleNamespace(
        ContentDetector=_content_detector,
        SceneManager=_SceneManager,
        open_video=_open_video,
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setitem(sys.modules, "scenedetect", scenedetect)
    analyzer = DefaultMediaAnalyzer(
        frame_reader=cast("OpenAIVisionFrameReader", cast("object", reader)),
        progress=events.append,
        maximum_frames=2,
    )

    readings = analyzer._read_frames(tmp_path / "media.mp4")  # pyright: ignore[reportPrivateUsage]

    assert tuple(reading.timestamp_seconds for reading in readings) == (1, 3)
    assert reader.progress_at_read == [(), (1.0,)]
    assert tuple(
        (event.current, event.total)
        for event in events
        if event.stage == IngestionProgressStage.FRAME_PROCESSING
    ) == ((1.0, 2.0), (2.0, 2.0))


@pytest.mark.parametrize("total_frames", [0, 1, 2_000, 2_001, 100_000])
def test__scene_probe_frame_skip_bounds_the_processed_frame_count(total_frames: int) -> None:
    frame_skip = media._scene_probe_frame_skip(total_frames, 2_000)  # pyright: ignore[reportPrivateUsage]

    processed = media._expected_scene_probes(total_frames, frame_skip)  # pyright: ignore[reportPrivateUsage]

    assert processed <= 2_000


def test__progress_content_detector_emits_explicit_ordered_endpoints() -> None:
    events: list[IngestionProgressEvent] = []
    detector = media._ProgressContentDetector(  # pyright: ignore[reportPrivateUsage]
        _SceneDetector(),
        events.append,
        expected_probes=3,
    )

    _ = detector.process_frame(object(), object())
    _ = detector.process_frame(object(), object())
    _ = detector.process_frame(object(), object())
    detector.complete()

    assert tuple((event.current, event.total) for event in events) == (
        (0.0, 100.0),
        (33.0, 100.0),
        (66.0, 100.0),
        (100.0, 100.0),
    )


def test__hybrid_frame_timestamps_covers_a_no_cut_timeline_with_bin_midpoints() -> None:
    timestamps = media._hybrid_frame_timestamps(100, [], 4)  # pyright: ignore[reportPrivateUsage]

    assert timestamps == [12.5, 37.5, 62.5, 87.5]


def test__hybrid_frame_timestamps_prefers_cuts_per_bin_and_caps_output() -> None:
    timestamps = media._hybrid_frame_timestamps(100, [1, 24, 26, 49, 51, 74, 76, 99], 4)  # pyright: ignore[reportPrivateUsage]

    assert timestamps == [1, 26, 51, 76]
    assert len(timestamps) <= 4
