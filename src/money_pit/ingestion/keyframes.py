"""Module containing keyframe selection with injected scene-detection and frame-extraction seams for the money_pit package."""

from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias

from money_pit.config import Config
from money_pit.ingestion.artifacts import Keyframe


SceneDetector: TypeAlias = Callable[[Path], list[float]]
FrameExtractor: TypeAlias = Callable[[Path, list[float], Path], list[Keyframe]]

_SECONDS_PER_MINUTE: int = 60
_SECONDS_PER_HOUR: int = 3600
_LOCATOR_FORMAT: str = "{hours:02d}:{minutes:02d}:{seconds:02d}"
_KEYFRAME_FILENAME_PREFIX: str = "kf_"
_KEYFRAME_FILENAME_SUFFIX: str = ".png"
_LOCATOR_PATH_SEPARATOR: str = "-"
_LOCATOR_TIME_SEPARATOR: str = ":"


def _seconds_to_locator(seconds: float) -> str:
    """Format a float second offset into a zero-padded HH:MM:SS locator, flooring fractional seconds."""
    whole = int(seconds)
    hours = whole // _SECONDS_PER_HOUR
    minutes = (whole % _SECONDS_PER_HOUR) // _SECONDS_PER_MINUTE
    remaining = whole % _SECONDS_PER_MINUTE
    return _LOCATOR_FORMAT.format(hours=hours, minutes=minutes, seconds=remaining)


def _cap_timestamps(timestamps: list[float], max_frames: int) -> list[float]:
    """Evenly subsample timestamps down to at most max_frames, preserving order and spread."""
    count = len(timestamps)
    if max_frames <= 0:
        return []
    if count <= max_frames:
        return timestamps
    if max_frames == 1:
        return [timestamps[0]]
    stride = (count - 1) / (max_frames - 1)
    return [timestamps[round(index * stride)] for index in range(max_frames)]


def select_keyframes(
    video_path: Path,
    out_dir: Path,
    detector: SceneDetector,
    extractor: FrameExtractor,
    *,
    max_frames: int,
) -> list[Keyframe]:
    """Detect scene boundaries, cap them to max_frames, and extract a keyframe per retained timestamp."""
    timestamps = detector(video_path)
    capped = _cap_timestamps(timestamps, max_frames)
    return extractor(video_path, capped, out_dir)


def make_scenedetect_detector(config: Config) -> SceneDetector:
    """Return a SceneDetector backed by PySceneDetect ContentDetector at the configured threshold."""

    def detect_scenes(video_path: Path) -> list[float]:  # pragma: no cover
        from scenedetect import ContentDetector
        from scenedetect import detect

        scenes = detect(str(video_path), ContentDetector(threshold=config.scene_detect_threshold))
        return [start.get_seconds() for start, _ in scenes]

    return detect_scenes


def make_opencv_extractor() -> FrameExtractor:
    """Return a FrameExtractor that seeks an OpenCV capture to each timestamp and writes one PNG per frame."""

    def extract_frames(video_path: Path, timestamps: list[float], out_dir: Path) -> list[Keyframe]:  # pragma: no cover
        import cv2

        capture = cv2.VideoCapture(str(video_path))
        keyframes: list[Keyframe] = []
        try:
            for timestamp in timestamps:
                _ = capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
                success, frame = capture.read()
                if not success:
                    continue
                locator = _seconds_to_locator(timestamp)
                filename = (
                    f"{_KEYFRAME_FILENAME_PREFIX}"
                    f"{locator.replace(_LOCATOR_TIME_SEPARATOR, _LOCATOR_PATH_SEPARATOR)}"
                    f"{_KEYFRAME_FILENAME_SUFFIX}"
                )
                image_path = out_dir / filename
                _ = cv2.imwrite(str(image_path), frame)
                keyframes.append(Keyframe(timestamp=timestamp, image_path=image_path, locator=locator))
        finally:
            capture.release()
        return keyframes

    return extract_frames
