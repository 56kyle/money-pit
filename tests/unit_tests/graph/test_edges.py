"""Tests for money_pit.graph.edges."""
from typing import TYPE_CHECKING

import pytest

from money_pit.graph.edges import EXECUTE
from money_pit.graph.edges import HALT
from money_pit.graph.edges import NO_ACTION
from money_pit.graph.edges import NOTIFY
from money_pit.graph.edges import PROCEED
from money_pit.graph.edges import VALIDATE
from money_pit.graph.edges import determination_gate
from money_pit.graph.edges import signal_gate
from money_pit.graph.edges import terminal_state_router
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.validation_results import ValidationStep


if TYPE_CHECKING:
    from money_pit.graph.state import PipelineState


def test_signal_gate_with_actionable_content() -> None:
    state: PipelineState = {"run_has_actionable_content": True}
    assert signal_gate(state) == PROCEED


def test_signal_gate_with_no_actionable_content() -> None:
    state: PipelineState = {"run_has_actionable_content": False}
    assert signal_gate(state) == NO_ACTION


def test_signal_gate_with_absent_key() -> None:
    state: PipelineState = {}
    assert signal_gate(state) == NO_ACTION


@pytest.mark.parametrize(
    "terminal_state",
    [TerminalState.ANALYSIS_HALT, TerminalState.NO_ACTION, TerminalState.VALIDATION_FAILED],
)
def test_terminal_state_router_with_terminal_state(terminal_state: TerminalState) -> None:
    state: PipelineState = {"terminal_state": terminal_state}
    assert terminal_state_router(state) == HALT


def test_terminal_state_router_with_none_terminal_state() -> None:
    state: PipelineState = {"terminal_state": None}
    assert terminal_state_router(state) == VALIDATE


def test_terminal_state_router_with_absent_key() -> None:
    state: PipelineState = {}
    assert terminal_state_router(state) == VALIDATE


def test_determination_gate_with_all_matched() -> None:
    step = ValidationStep(
        step_id="A001",
        status=ValidationStatus.MATCHED,
        tool_sequence=None,
        compensation_sequence=None,
        gap_description=None,
    )
    state: PipelineState = {"validation_steps": [step]}
    assert determination_gate(state) == EXECUTE


def test_determination_gate_with_unmatched_step() -> None:
    step = ValidationStep(
        step_id="A001",
        status=ValidationStatus.UNMATCHED,
        tool_sequence=None,
        compensation_sequence=None,
        gap_description=None,
    )
    state: PipelineState = {"validation_steps": [step]}
    assert determination_gate(state) == NOTIFY


def test_determination_gate_with_mixed_steps() -> None:
    matched = ValidationStep(
        step_id="A001",
        status=ValidationStatus.MATCHED,
        tool_sequence=None,
        compensation_sequence=None,
        gap_description=None,
    )
    unmatched = ValidationStep(
        step_id="A002",
        status=ValidationStatus.UNMATCHED,
        tool_sequence=None,
        compensation_sequence=None,
        gap_description=None,
    )
    state: PipelineState = {"validation_steps": [matched, unmatched]}
    assert determination_gate(state) == NOTIFY


def test_determination_gate_with_empty_list() -> None:
    state: PipelineState = {"validation_steps": []}
    assert determination_gate(state) == NOTIFY


def test_determination_gate_with_absent_key() -> None:
    state: PipelineState = {}
    assert determination_gate(state) == NOTIFY
