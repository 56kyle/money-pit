"""Unit tests for money_pit.ingestion.fetch — pure upload-date conversion and info-dict-to-SourceRef mapping."""

import pytest

from money_pit.ingestion.fetch import _build_source_ref
from money_pit.ingestion.fetch import _upload_date_to_iso
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


_RETRIEVED_AT: str = "2026-06-18T14:30:00Z"
_FALLBACK_URL: str = "https://www.youtube.com/watch?v=fallback"


def test__upload_date_to_iso_with_valid() -> None:
    assert _upload_date_to_iso("20260618") == "2026-06-18T00:00:00Z"


def test__upload_date_to_iso_with_none() -> None:
    assert _upload_date_to_iso(None) is None


def test__upload_date_to_iso_with_empty_string() -> None:
    assert _upload_date_to_iso("") is None


def test__upload_date_to_iso_with_malformed() -> None:
    with pytest.raises(ValueError):
        _upload_date_to_iso("2026")


def test__build_source_ref_with_valid(video_info: dict[str, object]) -> None:
    ref = _build_source_ref(video_info, _FALLBACK_URL, _RETRIEVED_AT)

    assert isinstance(ref, SourceRef)
    assert ref.source_id == f"yt:{video_info['id']}"
    assert ref.source_type is SourceType.NARRATED_VIDEO
    assert ref.title == video_info["title"]
    assert ref.url == video_info["webpage_url"]
    assert ref.published_at == _upload_date_to_iso(video_info["upload_date"])  # type: ignore[arg-type]
    assert ref.retrieved_at == _RETRIEVED_AT
    assert ref.locator is None


def test__build_source_ref_with_webpage_url_absent(video_info: dict[str, object]) -> None:
    info = dict(video_info)
    del info["webpage_url"]

    ref = _build_source_ref(info, _FALLBACK_URL, _RETRIEVED_AT)

    assert ref.url == _FALLBACK_URL


def test__build_source_ref_with_title_absent(video_info: dict[str, object]) -> None:
    info = dict(video_info)
    del info["title"]

    ref = _build_source_ref(info, _FALLBACK_URL, _RETRIEVED_AT)

    assert ref.title == ""


def test__build_source_ref_with_upload_date_absent(video_info: dict[str, object]) -> None:
    info = dict(video_info)
    del info["upload_date"]

    ref = _build_source_ref(info, _FALLBACK_URL, _RETRIEVED_AT)

    assert ref.published_at is None
