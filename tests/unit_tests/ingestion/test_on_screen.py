"""Unit tests for money_pit.ingestion.on_screen — the pure locator-stamping and VLM draft boundary."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from money_pit.ingestion.artifacts import Keyframe
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.on_screen import OnScreenDraft
from money_pit.ingestion.on_screen import _to_extraction
from money_pit.prompt_loader import system_prompt


@pytest.fixture
def keyframe() -> Keyframe:
    return Keyframe(timestamp=72.0, image_path=Path("x/kf.png"), locator="00:01:12")


def test__to_extraction_with_valid(keyframe: Keyframe) -> None:
    draft = OnScreenDraft(on_screen_text=["NVDA +3%", "Q3 Revenue"], cited_sources=["Source: Bloomberg"])

    extraction = _to_extraction(draft, keyframe)

    assert isinstance(extraction, OnScreenExtraction)
    assert extraction.locator == "00:01:12"
    assert extraction.on_screen_text == ["NVDA +3%", "Q3 Revenue"]
    assert extraction.cited_sources == ["Source: Bloomberg"]


def test__to_extraction_with_empty_draft(keyframe: Keyframe) -> None:
    draft = OnScreenDraft(on_screen_text=[], cited_sources=[])

    extraction = _to_extraction(draft, keyframe)

    assert extraction.locator == "00:01:12"
    assert extraction.on_screen_text == []
    assert extraction.cited_sources == []


def test_on_screen_draft_is_frozen() -> None:
    draft = OnScreenDraft(on_screen_text=["NVDA +3%"], cited_sources=["Source: Bloomberg"])

    with pytest.raises(ValidationError):
        draft.on_screen_text = ["mutated"]


def test_on_screen_draft_round_trips() -> None:
    draft = OnScreenDraft(
        on_screen_text=["NVDA +3%", "Q3 Revenue $35.1B"],
        cited_sources=["Source: Bloomberg", "Source: company filings"],
    )

    round_tripped = OnScreenDraft.model_validate_json(draft.model_dump_json())

    assert round_tripped == draft


def test_system_prompt_with_agent_video_onscreen() -> None:
    assert system_prompt("agent_video_onscreen").strip() != ""
