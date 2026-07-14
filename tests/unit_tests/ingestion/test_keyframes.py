"""Unit tests for money_pit.ingestion.keyframes — locator formatting, timestamp capping, and the injected select_keyframes seam."""

from pathlib import Path

import pytest

from money_pit.ingestion.artifacts import Keyframe
from money_pit.ingestion.keyframes import _cap_timestamps
from money_pit.ingestion.keyframes import _seconds_to_locator
from money_pit.ingestion.keyframes import select_keyframes


_MAX_FRAMES: int = 40
_DENSE_COUNT: int = 100


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00:00"),
        (5.4, "00:00:05"),
        (72.0, "00:01:12"),
        (3661.0, "01:01:01"),
    ],
)
def test__seconds_to_locator_with_valid(seconds: float, expected: str) -> None:
    assert _seconds_to_locator(seconds) == expected


def test__cap_timestamps_with_empty_is_identity() -> None:
    timestamps: list[float] = []

    assert _cap_timestamps(timestamps, _MAX_FRAMES) is timestamps


def test__cap_timestamps_with_single_element_is_identity() -> None:
    timestamps = [7.0]

    assert _cap_timestamps(timestamps, _MAX_FRAMES) is timestamps


def test__cap_timestamps_with_exactly_max_frames_is_identity() -> None:
    timestamps = [float(index) for index in range(_MAX_FRAMES)]

    assert _cap_timestamps(timestamps, _MAX_FRAMES) is timestamps


def test__cap_timestamps_with_more_than_max_frames_returns_max_frames() -> None:
    timestamps = [float(index) for index in range(_DENSE_COUNT)]

    capped = _cap_timestamps(timestamps, _MAX_FRAMES)

    assert len(capped) == _MAX_FRAMES


def test__cap_timestamps_with_more_than_max_frames_preserves_endpoints() -> None:
    timestamps = [float(index) for index in range(_DENSE_COUNT)]

    capped = _cap_timestamps(timestamps, _MAX_FRAMES)

    assert capped[0] == timestamps[0]
    assert capped[-1] == timestamps[-1]


def test__cap_timestamps_with_more_than_max_frames_is_non_decreasing() -> None:
    timestamps = [float(index) for index in range(_DENSE_COUNT)]

    capped = _cap_timestamps(timestamps, _MAX_FRAMES)

    assert all(earlier <= later for earlier, later in zip(capped, capped[1:], strict=False))


def test__cap_timestamps_with_max_frames_one_and_single_element_is_identity() -> None:
    timestamps = [7.0]

    capped = _cap_timestamps(timestamps, 1)

    assert capped == [7.0]
    assert len(capped) == 1


def test__cap_timestamps_with_max_frames_one_and_many_returns_first() -> None:
    timestamps = [1.0, 2.0, 3.0]

    assert _cap_timestamps(timestamps, 1) == [1.0]


def test__cap_timestamps_with_non_positive_max_frames_returns_empty() -> None:
    timestamps = [1.0, 2.0, 3.0]

    assert _cap_timestamps(timestamps, 0) == []


def _recording_detector(timestamps: list[float], calls: list[Path]) -> object:
    def detect(video_path: Path) -> list[float]:
        calls.append(video_path)
        return list(timestamps)

    return detect


def _recording_extractor(calls: list[tuple[Path, list[float], Path]]) -> object:
    def extract(video_path: Path, timestamps: list[float], out_dir: Path) -> list[Keyframe]:
        calls.append((video_path, list(timestamps), out_dir))
        return [
            Keyframe(
                timestamp=timestamp,
                image_path=out_dir / f"kf_{index}.png",
                locator=_seconds_to_locator(timestamp),
            )
            for index, timestamp in enumerate(timestamps)
        ]

    return extract


def test_select_keyframes_with_valid_returns_extractor_keyframes(tmp_path: Path) -> None:
    video_path = tmp_path / "clip.mp4"
    out_dir = tmp_path / "frames"
    detected = [0.0, 12.0, 30.0]
    extractor_calls: list[tuple[Path, list[float], Path]] = []
    detector = _recording_detector(detected, [])
    extractor = _recording_extractor(extractor_calls)

    keyframes = select_keyframes(video_path, out_dir, detector, extractor, max_frames=_MAX_FRAMES)

    assert [keyframe.timestamp for keyframe in keyframes] == detected


def test_select_keyframes_with_valid_calls_detector_with_video_path(tmp_path: Path) -> None:
    video_path = tmp_path / "clip.mp4"
    out_dir = tmp_path / "frames"
    detector_calls: list[Path] = []
    detector = _recording_detector([0.0, 12.0], detector_calls)
    extractor = _recording_extractor([])

    select_keyframes(video_path, out_dir, detector, extractor, max_frames=_MAX_FRAMES)

    assert detector_calls == [video_path]


def test_select_keyframes_with_valid_threads_video_path_and_out_dir_to_extractor(tmp_path: Path) -> None:
    video_path = tmp_path / "clip.mp4"
    out_dir = tmp_path / "frames"
    extractor_calls: list[tuple[Path, list[float], Path]] = []
    detector = _recording_detector([0.0, 12.0, 30.0], [])
    extractor = _recording_extractor(extractor_calls)

    select_keyframes(video_path, out_dir, detector, extractor, max_frames=_MAX_FRAMES)

    assert len(extractor_calls) == 1
    received_video_path, _, received_out_dir = extractor_calls[0]
    assert received_video_path == video_path
    assert received_out_dir == out_dir


def test_select_keyframes_with_more_detections_than_max_frames_caps_before_extractor(tmp_path: Path) -> None:
    video_path = tmp_path / "clip.mp4"
    out_dir = tmp_path / "frames"
    detected = [float(index) for index in range(_DENSE_COUNT)]
    extractor_calls: list[tuple[Path, list[float], Path]] = []
    detector = _recording_detector(detected, [])
    extractor = _recording_extractor(extractor_calls)

    keyframes = select_keyframes(video_path, out_dir, detector, extractor, max_frames=_MAX_FRAMES)

    _, received_timestamps, _ = extractor_calls[0]
    assert len(received_timestamps) == _MAX_FRAMES
    assert len(keyframes) == _MAX_FRAMES
    assert received_timestamps[0] == detected[0]
    assert received_timestamps[-1] == detected[-1]
