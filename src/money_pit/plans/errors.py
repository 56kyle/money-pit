"""Module containing explicit portfolio-plan lifecycle failures."""


class PlanLifecycleError(Exception):
    """Base class for portfolio-plan lifecycle failures."""


class PlanHashMismatchError(PlanLifecycleError):
    """Raised when persisted plan content does not match its digest."""


class PlanExpiredError(PlanLifecycleError):
    """Raised when execution is attempted after a plan expires."""


class PlanStateDriftError(PlanLifecycleError):
    """Raised when current decision state differs from plan-bound state."""


class PlanApprovalRequiredError(PlanLifecycleError):
    """Raised when a plan lacks the exact approval required for execution."""


class PlanRejectedError(PlanLifecycleError):
    """Raised when execution is attempted with a rejection decision."""


class ExecutionDisabledError(PlanLifecycleError):
    """Raised when the global kill switch or observation mode prevents execution."""


class PlanGateFailedError(PlanLifecycleError):
    """Raised when an evidence, constraint, or autonomous policy gate fails."""
