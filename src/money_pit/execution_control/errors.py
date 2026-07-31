"""Module containing explicit execution-control failures."""

from money_pit.execution_control.models import PreflightResult


class ExecutionControlError(Exception):
    """Base class for execution-control failures."""


class PlanNotFoundError(ExecutionControlError):
    """Raised when a requested durable portfolio plan does not exist."""


class PlanDecisionConflictError(ExecutionControlError):
    """Raised when an immutable plan decision conflicts with stored state."""


class ExecutionDisabledError(ExecutionControlError):
    """Raised when the global kill switch prevents a capital write."""


class PreflightDeniedError(ExecutionControlError):
    """Raised when one or more deterministic pre-execution gates fail."""

    def __init__(self, result: PreflightResult) -> None:
        """Initialize the error with its complete typed preflight result."""
        self.result: PreflightResult = result
        codes: str = ", ".join(denial.code.value for denial in result.denials)
        super().__init__(f"Portfolio plan {result.plan_id!r} failed preflight: {codes}.")


class StateCheckUnavailableError(ExecutionControlError):
    """Raised when current portfolio or market state cannot be read."""
