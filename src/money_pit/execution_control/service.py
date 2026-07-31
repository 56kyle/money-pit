"""Module containing approval, rejection, and kill-switch application services."""

import uuid
from datetime import datetime

from money_pit.execution_control.errors import PlanNotFoundError
from money_pit.execution_control.models import ApprovalDecision
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.protocols import DecisionRepository
from money_pit.execution_control.protocols import KillSwitchStore
from money_pit.execution_control.protocols import PlanRepository
from money_pit.plans.lifecycle import PlanDecision
from money_pit.plans.lifecycle import approve_plan
from money_pit.plans.lifecycle import reject_plan
from money_pit.schemas.portfolio_plan import PortfolioPlan


def require_plan(plans: PlanRepository, plan_id: str) -> PortfolioPlan:
    """Return a durable plan or raise PlanNotFoundError."""
    plan: PortfolioPlan | None = plans.get(plan_id)
    if plan is None:
        raise PlanNotFoundError(f"Portfolio plan {plan_id!r} does not exist.")
    return plan


def record_plan_decision(
    plans: PlanRepository,
    decisions: DecisionRepository,
    *,
    plan_id: str,
    decision: ApprovalDecision,
    decided_at: datetime,
    actor: str,
    reason: str | None,
) -> PlanDecision:
    """Persist an immutable operator decision covering the plan's current exact hash."""
    plan: PortfolioPlan = require_plan(plans, plan_id)
    decision_id: str = str(uuid.uuid4())
    decision_value: object = decision
    if decision_value is ApprovalDecision.APPROVED:
        record: PlanDecision = approve_plan(
            plan,
            decision_id=decision_id,
            decided_at=decided_at,
            decided_by=actor,
        )
    elif decision_value is ApprovalDecision.REJECTED:
        if reason is None or not reason.strip():
            raise ValueError("A non-blank reason is required to reject a plan.")
        record = reject_plan(plan, decision_id=decision_id, decided_at=decided_at, decided_by=actor, reason=reason)
    else:
        raise AssertionError(f"Unhandled approval decision: {decision_value!r}")

    decisions.append(record)
    return record


def disable_execution(
    kill_switch: KillSwitchStore,
    *,
    changed_at: datetime,
    actor: str,
    reason: str,
) -> ExecutionControlState:
    """Durably disable all capital writes."""
    state: ExecutionControlState = ExecutionControlState(
        disabled=True,
        changed_at=changed_at,
        actor=actor,
        reason=reason,
    )
    kill_switch.disable(state)
    return state
