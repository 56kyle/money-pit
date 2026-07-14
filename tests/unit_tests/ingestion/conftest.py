"""Fixtures shared across the video-ingestion unit tests (captions, fuse, artifacts)."""

import json
from pathlib import Path

import pytest
from pytest import FixtureRequest

from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


_VIDEO_DATA_SUBPATH: tuple[str, str] = ("adapters", "video")
_UPLOADER_VTT_NAME: str = "uploader.en.vtt"
_AUTO_VTT_NAME: str = "auto.en.vtt"
_METADATA_NAME: str = "metadata.json"


@pytest.fixture
def video_data_folder(request: FixtureRequest, data_folder: Path) -> Path:
    return getattr(request, "param", data_folder.joinpath(*_VIDEO_DATA_SUBPATH))


@pytest.fixture
def metadata_json_path(request: FixtureRequest, video_data_folder: Path) -> Path:
    return getattr(request, "param", video_data_folder / _METADATA_NAME)


@pytest.fixture
def video_info(request: FixtureRequest, metadata_json_path: Path) -> dict[str, object]:
    return getattr(request, "param", json.loads(metadata_json_path.read_text(encoding="utf-8")))


@pytest.fixture
def uploader_vtt_path(request: FixtureRequest, video_data_folder: Path) -> Path:
    return getattr(request, "param", video_data_folder / _UPLOADER_VTT_NAME)


@pytest.fixture
def auto_vtt_path(request: FixtureRequest, video_data_folder: Path) -> Path:
    return getattr(request, "param", video_data_folder / _AUTO_VTT_NAME)


@pytest.fixture
def uploader_vtt_text(request: FixtureRequest, uploader_vtt_path: Path) -> str:
    return getattr(request, "param", uploader_vtt_path.read_text(encoding="utf-8"))


@pytest.fixture
def auto_vtt_text(request: FixtureRequest, auto_vtt_path: Path) -> str:
    return getattr(request, "param", auto_vtt_path.read_text(encoding="utf-8"))


@pytest.fixture
def source_ref(request: FixtureRequest) -> SourceRef:
    return getattr(
        request,
        "param",
        SourceRef(
            source_id="yt_test_001",
            source_type=SourceType.NARRATED_VIDEO,
            title="Test Video: NVDA Thesis Review",
            url="https://www.youtube.com/watch?v=test",
            published_at="2026-01-15T12:00:00Z",
            retrieved_at="2026-06-18T14:30:00Z",
            locator=None,
        ),
    )
