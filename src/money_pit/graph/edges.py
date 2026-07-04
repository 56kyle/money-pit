"""Conditional edge functions: signal_gate, terminal_state_router, determination_router, post_notification_router."""

from typing import Literal

from money_pit.graph.state import PipelineState
from money_pit.schemas.enums import TerminalState


PROCEED: Literal["proceed"] = "proceed"
NO_ACTION: Literal["no_action"] = "no_action"
VALIDATE: Literal["validate"] = "validate"
EXECUTE: Literal["execute"] = "execute"
NOTIFY: Literal["notify"] = "notify"
FINALIZE: Literal["finalize"] = "finalize"
TERMINATE: Literal["terminate"] = "terminate"


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


def post_notification_router(state: PipelineState) -> Literal["terminate", "finalize"]:
    """Route after notification: ANALYSIS_HALT ends the run, VALIDATION_ERROR rejoins the finalizer."""
    if state.get("terminal_state") is TerminalState.VALIDATION_ERROR:
        return FINALIZE
    return TERMINATE
