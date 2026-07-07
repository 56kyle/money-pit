"""Tests for money_pit.prompt_loader."""

import pytest

from money_pit.prompt_loader import system_prompt


def test_system_prompt_with_valid() -> None:
    assert system_prompt("agent_1").strip() != ""


def test_system_prompt_with_unknown_name() -> None:
    with pytest.raises(FileNotFoundError):
        system_prompt("does_not_exist")


def test_system_prompt_is_cached() -> None:
    assert system_prompt("agent_2") is system_prompt("agent_2")
