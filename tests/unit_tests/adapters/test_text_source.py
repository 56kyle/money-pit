"""Unit tests for the deterministic thesis-file front-end: read_thesis, text_source_id, build_text_payload."""

from pathlib import Path

import pytest

from money_pit.adapters.text_source import EmptyThesisError
from money_pit.adapters.text_source import ThesisFileNotReadableError
from money_pit.adapters.text_source import build_text_payload
from money_pit.adapters.text_source import read_thesis
from money_pit.adapters.text_source import text_source_id
from money_pit.schemas.enums import SourceType


@pytest.fixture
def thesis_path(tmp_path: Path) -> Path:
    path = tmp_path / "thesis.txt"
    _ = path.write_text("  NVDA is well-positioned for AI infrastructure buildout.\n", encoding="utf-8")
    return path


@pytest.fixture
def whitespace_thesis_path(tmp_path: Path) -> Path:
    path = tmp_path / "empty.txt"
    _ = path.write_text("   \n\t  \n", encoding="utf-8")
    return path


@pytest.fixture
def missing_thesis_path(tmp_path: Path) -> Path:
    return tmp_path / "does_not_exist.txt"


def test_read_thesis_with_valid(thesis_path: Path) -> None:
    assert read_thesis(thesis_path) == "NVDA is well-positioned for AI infrastructure buildout."


def test_read_thesis_with_whitespace_only(whitespace_thesis_path: Path) -> None:
    with pytest.raises(EmptyThesisError):
        _ = read_thesis(whitespace_thesis_path)


def test_read_thesis_with_missing_path(missing_thesis_path: Path) -> None:
    with pytest.raises(ThesisFileNotReadableError):
        _ = read_thesis(missing_thesis_path)


def test_text_source_id_has_note_prefix_and_eight_hex() -> None:
    source_id = text_source_id("A thesis body.")

    prefix, _, digest = source_id.partition(":")
    assert prefix == "note"
    assert len(digest) == 8
    assert all(character in "0123456789abcdef" for character in digest)


def test_text_source_id_is_deterministic() -> None:
    assert text_source_id("A thesis body.") == text_source_id("A thesis body.")


def test_text_source_id_differs_for_different_bodies() -> None:
    assert text_source_id("A thesis body.") != text_source_id("A different thesis body.")


@pytest.fixture
def built_payload_body() -> str:
    return "NVDA is well-positioned for AI infrastructure buildout."


def test_build_text_payload_uses_content_addressed_source_id(built_payload_body: str) -> None:
    payload = build_text_payload(
        built_payload_body,
        slug="2026-06-18_14-30-00",
        title="NVDA Thesis Note",
        retrieved_at="2026-06-18T14:30:00Z",
    )
    assert payload.source_ref.source_id == text_source_id(built_payload_body)


def test_build_text_payload_mints_manual_note_source_type(built_payload_body: str) -> None:
    payload = build_text_payload(
        built_payload_body,
        slug="2026-06-18_14-30-00",
        title="NVDA Thesis Note",
        retrieved_at="2026-06-18T14:30:00Z",
    )
    assert payload.source_ref.source_type is SourceType.MANUAL_NOTE


def test_build_text_payload_nulls_url_published_at_and_locator(built_payload_body: str) -> None:
    payload = build_text_payload(
        built_payload_body,
        slug="2026-06-18_14-30-00",
        title="NVDA Thesis Note",
        retrieved_at="2026-06-18T14:30:00Z",
    )
    assert payload.source_ref.url is None
    assert payload.source_ref.published_at is None
    assert payload.source_ref.locator is None


def test_build_text_payload_carries_passed_fields(built_payload_body: str) -> None:
    payload = build_text_payload(
        built_payload_body,
        slug="2026-06-18_14-30-00",
        title="NVDA Thesis Note",
        retrieved_at="2026-06-18T14:30:00Z",
    )
    assert payload.slug == "2026-06-18_14-30-00"
    assert payload.body == built_payload_body
    assert payload.source_ref.title == "NVDA Thesis Note"
    assert payload.source_ref.retrieved_at == "2026-06-18T14:30:00Z"
