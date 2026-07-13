"""Tests for money_pit.graph.edges — the conditional routers (wave S5 / ADR 0006, wave D fill-observability)."""

from typing import TYPE_CHECKING

import pytest

from money_pit.graph.edges import EXECUTE
from money_pit.graph.edges import FINALIZE
from money_pit.graph.edges import NO_ACTION
from money_pit.graph.edges import NOTIFY
from money_pit.graph.edges import PROCEED
from money_pit.graph.edges import TERMINATE
from money_pit.graph.edges import VALIDATE
from money_pit.graph.edges import determination_router
from money_pit.graph.edges import execution_outcome_router
from money_pit.graph.edges import post_notification_router
from money_pit.graph.edges import signal_gate
from money_pit.graph.edges import terminal_state_router
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import TerminalState


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


def test_terminal_state_router_with_none_terminal_state() -> None:
    state: PipelineState = {"terminal_state": None}
    assert terminal_state_router(state) == VALIDATE


def test_terminal_state_router_with_absent_key() -> None:
    state: PipelineState = {}
    assert terminal_state_router(state) == VALIDATE


def test_terminal_state_router_with_no_action() -> None:
    state: PipelineState = {"terminal_state": TerminalState.NO_ACTION}
    assert terminal_state_router(state) == NO_ACTION


def test_terminal_state_router_with_analysis_halt() -> None:
    state: PipelineState = {"terminal_state": TerminalState.ANALYSIS_HALT}
    assert terminal_state_router(state) == NOTIFY


def test_determination_router_with_proceed_leaves_terminal_state_none() -> None:
    state: PipelineState = {"terminal_state": None}
    assert determination_router(state) == EXECUTE


def test_determination_router_with_validation_error() -> None:
    state: PipelineState = {"terminal_state": TerminalState.VALIDATION_ERROR}
    assert determination_router(state) == NOTIFY


def test_determination_router_with_orchestration_error() -> None:
    state: PipelineState = {"terminal_state": TerminalState.ORCHESTRATION_ERROR}
    assert determination_router(state) == FINALIZE


def test_execution_outcome_router_with_executed_incomplete() -> None:
    state: PipelineState = {"execution_outcome": ExecutionOutcome.EXECUTED_INCOMPLETE}
    assert execution_outcome_router(state) == NOTIFY


@pytest.mark.parametrize(
    "outcome",
    [
        ExecutionOutcome.EXECUTED_CLEAN,
        ExecutionOutcome.EXECUTION_FAILED,
        ExecutionOutcome.PARTIAL_COMPENSATED,
        ExecutionOutcome.COMPENSATION_FAILED,
    ],
)
def test_execution_outcome_router_with_non_incomplete_outcome(outcome: ExecutionOutcome) -> None:
    state: PipelineState = {"execution_outcome": outcome}
    assert execution_outcome_router(state) == FINALIZE


def test_execution_outcome_router_with_none() -> None:
    state: PipelineState = {"execution_outcome": None}
    assert execution_outcome_router(state) == FINALIZE


def test_execution_outcome_router_with_absent_key() -> None:
    state: PipelineState = {}
    assert execution_outcome_router(state) == FINALIZE


def test_post_notification_router_with_validation_error() -> None:
    state: PipelineState = {"terminal_state": TerminalState.VALIDATION_ERROR}
    assert post_notification_router(state) == FINALIZE


def test_post_notification_router_with_analysis_halt() -> None:
    state: PipelineState = {"terminal_state": TerminalState.ANALYSIS_HALT}
    assert post_notification_router(state) == TERMINATE


def test_post_notification_router_with_none_terminal_state() -> None:
    state: PipelineState = {"terminal_state": None}
    assert post_notification_router(state) == FINALIZE


def test_post_notification_router_with_absent_key() -> None:
    state: PipelineState = {}
    assert post_notification_router(state) == FINALIZE
