"""Conditional edge functions: signal_gate, terminal_state_router, determination_gate."""
from typing import TYPE_CHECKING
from typing import Literal

from money_pit.graph.state import PipelineState
from money_pit.schemas.enums import ValidationStatus


if TYPE_CHECKING:
    from money_pit.schemas.validation_results import ValidationStep

PROCEED = "proceed"
NO_ACTION = "no_action"
VALIDATE = "validate"
HALT = "halt"
EXECUTE = "execute"
NOTIFY = "notify"


def signal_gate(state: PipelineState) -> Literal["proceed", "no_action"]:
    """Route on whether the current run has actionable content."""
    if state.get("run_has_actionable_content", False):
        return PROCEED
    return NO_ACTION


def terminal_state_router(state: PipelineState) -> Literal["validate", "halt"]:
    """Route on whether a terminal state has already been set."""
    if state.get("terminal_state") is not None:
        return HALT
    return VALIDATE


def determination_gate(state: PipelineState) -> Literal["execute", "notify"]:
    """Route on whether all validation steps are matched."""
    steps: list[ValidationStep] = state.get("validation_steps", [])
    if steps and all(step.status == ValidationStatus.MATCHED for step in steps):
        return EXECUTE
    return NOTIFY
