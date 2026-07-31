"""Module containing injected execution-authority protocols."""

from datetime import datetime
from typing import Protocol

from money_pit.execution_control.models import ExecutionControlState
from money_pit.plans.lifecycle import AuthorizationContext
from money_pit.plans.lifecycle import PlanDecision
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan


class PlanRepository(Protocol):
    """Load durable portfolio plans by opaque identifier."""

    def get(self, plan_id: str) -> PortfolioPlan | None:
        """Return a plan, or None when the identifier is unknown."""
        ...


class DecisionRepository(Protocol):
    """Append and query durable plan approval decisions."""

    def append(self, record: PlanDecision) -> None:
        """Persist one immutable approval or rejection record."""
        ...

    def latest_for(self, plan_id: str) -> PlanDecision | None:
        """Return the latest decision for a plan, or None."""
        ...


class KillSwitchStore(Protocol):
    """Read and durably update the global execution kill switch."""

    def get_control_state(self) -> ExecutionControlState:
        """Return the current global execution-control state."""
        ...

    def disable(self, state: ExecutionControlState) -> None:
        """Persist a disabled state."""
        ...

    def enable(self, state: ExecutionControlState) -> None:
        """Persist an enabled state without authorizing a plan."""
        ...


class AuthorizationContextProvider(Protocol):
    """Read trusted current state for immediate plan authorization."""

    def current(self, plan: PortfolioPlan) -> AuthorizationContext:
        """Return current state or raise a typed availability failure."""
        ...


class AutonomousCoverageEvaluator(Protocol):
    """Decide whether a versioned autonomous policy covers an exact plan."""

    def covers(self, plan: PortfolioPlan, policy: ExecutionPolicy) -> bool:
        """Return True only when every proposed action is explicitly covered."""
        ...


class ExecutionClaimRepository(Protocol):
    """Provide durable idempotency claims for plan trades."""

    def claim(self, plan_hash: str, trade_identity: str, *, claimed_at: datetime) -> None:
        """Claim an unexecuted trade or raise when it already exists."""
        ...

    def update(
        self,
        plan_hash: str,
        trade_identity: str,
        *,
        status: str,
        updated_at: datetime,
        broker_order_id: str | None,
    ) -> None:
        """Persist a claimed trade's latest execution phase."""
        ...
