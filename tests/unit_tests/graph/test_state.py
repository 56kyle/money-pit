"""Tests for money_pit.graph.state — the shared PipelineState accessors and their fail-closed guards."""

from pathlib import Path

import pytest

from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step


def test_require_working_dir_with_present_key_returns_path() -> None:
    state: PipelineState = {"working_dir": "some/working/dir"}
    assert require_working_dir(state) == Path("some/working/dir")


def test_require_working_dir_with_missing_key_raises() -> None:
    with pytest.raises(ValueError):
        _ = require_working_dir({})


def test_require_slug_with_present_key_returns_slug() -> None:
    state: PipelineState = {"slug": "my-slug"}
    assert require_slug(state) == "my-slug"


def test_require_slug_with_missing_key_raises() -> None:
    with pytest.raises(ValueError):
        _ = require_slug({})


def test_with_completed_step_with_existing_steps_appends() -> None:
    state: PipelineState = {"completed_steps": ["a", "b"]}
    assert with_completed_step(state, "c") == ["a", "b", "c"]


def test_with_completed_step_with_absent_key_returns_singleton() -> None:
    assert with_completed_step({}, "c") == ["c"]


def test_with_completed_step_with_empty_list_returns_singleton() -> None:
    state: PipelineState = {"completed_steps": []}
    assert with_completed_step(state, "c") == ["c"]


def test_with_completed_step_with_existing_steps_does_not_mutate_input() -> None:
    original = ["a", "b"]
    state: PipelineState = {"completed_steps": original}
    _ = with_completed_step(state, "c")
    assert original == ["a", "b"]
