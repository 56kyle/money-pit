"""Module containing injected execution-authority protocols."""

from datetime import datetime
from typing import Protocol

from money_pit.execution_control.models import ExecutionClaim
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.models import ExecutionEvent
from money_pit.execution_control.models import PreflightDenial
from money_pit.plans.lifecycle import PlanDecision
from money_pit.plans.lifecycle import RejectionRecord
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

    def rejection_for(self, plan_id: str, plan_hash: str) -> RejectionRecord | None:
        """Return an exact-hash rejection, which is a terminal veto."""
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


class AutonomousEligibilityEvaluator(Protocol):
    """Evaluate staged autonomous-execution evidence for one exact plan."""

    def denials(self, plan: PortfolioPlan, policy: ExecutionPolicy) -> tuple[PreflightDenial, ...]:
        """Return every reason autonomous execution is unavailable."""
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

    def nonterminal_claims(self) -> tuple[ExecutionClaim, ...]:
        """Return every claim that still requires broker reconciliation."""
        ...


class ExecutionJournalRepository(Protocol):
    """Append and query immutable execution lifecycle events."""

    def append_event(self, event: ExecutionEvent) -> None:
        """Persist one event before allowing the next side effect."""
        ...

    def events_for_plan(self, plan_hash: str) -> tuple[ExecutionEvent, ...]:
        """Return one plan's events in deterministic append order."""
        ...
