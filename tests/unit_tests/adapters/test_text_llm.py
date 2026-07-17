"""Unit tests for the TextPayload model and the A1 text user-message builder."""

import json

import pytest
from pydantic import ValidationError

from money_pit.adapters.text_llm import TextPayload
from money_pit.adapters.text_llm import _build_user_message
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


@pytest.fixture
def source_ref() -> SourceRef:
    return SourceRef(
        source_id="note:ab12cd34",
        source_type=SourceType.MANUAL_NOTE,
        title="NVDA Thesis Note",
        url=None,
        published_at=None,
        retrieved_at="2026-06-18T14:30:00Z",
        locator=None,
    )


@pytest.fixture
def text_payload(source_ref: SourceRef) -> TextPayload:
    return TextPayload(
        slug="2026-06-18_14-30-00",
        source_ref=source_ref,
        body=(
            "NVDA is well-positioned for AI infrastructure buildout. "
            "The data center segment continues to show 200%+ growth. "
            "Risk: AMD competition increasing."
        ),
    )


def test_text_payload_serializes_round_trip(text_payload: TextPayload) -> None:
    """TextPayload survives a JSON round-trip and compares equal to the original."""
    round_tripped = TextPayload.model_validate_json(text_payload.model_dump_json())

    assert round_tripped == text_payload


def test_text_payload_rejects_extra_fields(source_ref: SourceRef) -> None:
    """extra='forbid' rejects an unexpected field at construction."""
    with pytest.raises(ValidationError):
        _ = TextPayload(  # pyright: ignore[reportCallIssue]
            slug="2026-06-18_14-30-00",
            source_ref=source_ref,
            body="A thesis.",
            unexpected="nope",
        )


def test_text_payload_is_frozen(text_payload: TextPayload) -> None:
    """frozen=True rejects mutation of an existing field."""
    with pytest.raises(ValidationError):
        text_payload.body = "mutated"


def test__build_user_message_renders_metadata_and_thesis(text_payload: TextPayload) -> None:
    """The message carries the Source Metadata header, the source_ref JSON, the Thesis header, and the body."""
    message = _build_user_message(text_payload)

    assert "## Source Metadata" in message
    assert text_payload.source_ref.model_dump_json(indent=2) in message
    assert "## Thesis\n" in message
    assert text_payload.body in message


def test__build_user_message_omits_video_only_sections(text_payload: TextPayload) -> None:
    """A text thesis has no on-screen-text or transcript-provenance sections."""
    message = _build_user_message(text_payload)

    assert "## On-Screen Text" not in message
    assert "## Transcript Provenance" not in message


def test__build_user_message_metadata_json_is_parseable(text_payload: TextPayload) -> None:
    """The metadata block embeds the source_ref as valid JSON."""
    message = _build_user_message(text_payload)
    metadata_block = message.split("## Thesis\n")[0].removeprefix("## Source Metadata\n").strip()

    assert json.loads(metadata_block)["source_id"] == "note:ab12cd34"
