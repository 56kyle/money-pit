"""Module containing the conditional edge functions (recovery_router, signal_gate, terminal_state_router, determination_router, execution_outcome_router, post_notification_router) for the money_pit package."""

from typing import Literal

from money_pit.graph.state import PipelineState
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import RecoveryDecision
from money_pit.schemas.enums import TerminalState


PROCEED: Literal["proceed"] = "proceed"
NO_ACTION: Literal["no_action"] = "no_action"
VALIDATE: Literal["validate"] = "validate"
EXECUTE: Literal["execute"] = "execute"
NOTIFY: Literal["notify"] = "notify"
FINALIZE: Literal["finalize"] = "finalize"
TERMINATE: Literal["terminate"] = "terminate"
HALT: Literal["halt"] = "halt"


def recovery_router(state: PipelineState) -> Literal["proceed", "halt"]:
    """Route the entry fork: a still-open prior order halts the new run before planning; otherwise proceed."""
    if state.get("recovery_decision") is RecoveryDecision.HALT:
        return HALT
    return PROCEED


def signal_gate(state: PipelineState) -> Literal["proceed", "no_action"]:
    """Route on whether the current run has actionable content."""
    if state.get("run_has_actionable_content", False):
        return PROCEED
    return NO_ACTION


def terminal_state_router(state: PipelineState) -> Literal["validate", "no_action", "notify"]:
    """Route the post-analysis fork on the pre-execution terminal state."""
    terminal_state: TerminalState | None = state.get("terminal_state")
    if terminal_state is None:
        return VALIDATE
    if terminal_state is TerminalState.NO_ACTION:
        return NO_ACTION
    return NOTIFY


def determination_router(state: PipelineState) -> Literal["execute", "notify", "finalize"]:
    """Route the post-determination fork: PROCEED→execute, VALIDATION_ERROR→notify, parse-failure→finalize."""
    terminal_state: TerminalState | None = state.get("terminal_state")
    if terminal_state is TerminalState.ORCHESTRATION_ERROR:
        return FINALIZE
    if terminal_state is TerminalState.VALIDATION_ERROR:
        return NOTIFY
    return EXECUTE


def execution_outcome_router(state: PipelineState) -> Literal["notify", "finalize"]:
    """Route after execution: an incomplete execution (open/partial legs) notifies the owner before finalizing; clean and cleanly-failed runs finalize directly."""
    if state.get("execution_outcome") is ExecutionOutcome.EXECUTED_INCOMPLETE:
        return NOTIFY
    return FINALIZE


def post_notification_router(state: PipelineState) -> Literal["terminate", "finalize"]:
    """Route after notification: ANALYSIS_HALT ends the run; VALIDATION_ERROR and execution-incomplete (terminal_state None) rejoin the finalizer."""
    if state.get("terminal_state") is TerminalState.ANALYSIS_HALT:
        return TERMINATE
    return FINALIZE
