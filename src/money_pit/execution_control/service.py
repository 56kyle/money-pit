"""Module containing approval, rejection, and kill-switch application services."""

import uuid
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.execution_control.errors import PlanNotFoundError
from money_pit.execution_control.models import ApprovalDecision
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.protocols import DecisionRepository
from money_pit.execution_control.protocols import KillSwitchStore
from money_pit.execution_control.protocols import PlanRepository
from money_pit.plans.lifecycle import PlanDecision
from money_pit.plans.lifecycle import approve_plan
from money_pit.plans.lifecycle import reject_plan
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan


class ApprovalBinding(BaseModel):
    """Exact active execution and broker state covered by an approval."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    execution_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy: ExecutionPolicy
    portfolio: PortfolioStateSnapshot
    committed_turnover: float = Field(ge=0, le=1)


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
    approval_binding: ApprovalBinding | None = None,
) -> PlanDecision:
    """Persist an immutable operator decision covering the plan's current exact hash."""
    plan: PortfolioPlan = require_plan(plans, plan_id)
    decision_id: str = str(uuid.uuid4())
    decision_value: object = decision
    if decision_value is ApprovalDecision.APPROVED:
        if approval_binding is None:
            raise ValueError("Approval requires exact execution-policy and broker-account binding.")
        portfolio = approval_binding.portfolio.payload
        if portfolio.account_status.upper() != "ACTIVE" or portfolio.trading_blocked:
            raise ValueError("Approval requires an active, unblocked broker account.")
        if portfolio.broker_environment is not approval_binding.execution_policy.broker_environment:
            raise ValueError("Approval broker environment differs from the active execution policy.")
        if portfolio.account_id != plan.payload.account_id:
            raise ValueError("Approval broker account differs from the plan-bound account.")
        if portfolio.broker_environment is not plan.payload.broker_environment:
            raise ValueError("Approval broker environment differs from the plan-bound environment.")
        record: PlanDecision = approve_plan(
            plan,
            decision_id=decision_id,
            decided_at=decided_at,
            decided_by=actor,
            execution_config_hash=approval_binding.execution_config_hash,
            execution_policy=approval_binding.execution_policy,
            account_id=portfolio.account_id,
            committed_turnover_at_approval=approval_binding.committed_turnover,
        )
    else:
        if reason is None or not reason.strip():
            raise ValueError("A non-blank reason is required to reject a plan.")
        record = reject_plan(plan, decision_id=decision_id, decided_at=decided_at, decided_by=actor, reason=reason)

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
    current: ExecutionControlState = kill_switch.get_control_state()
    state: ExecutionControlState = ExecutionControlState(
        disabled=True,
        changed_at=changed_at,
        actor=actor,
        reason=reason,
        policy_version=current.policy_version,
    )
    kill_switch.disable(state)
    return state


def enable_execution(
    kill_switch: KillSwitchStore,
    *,
    changed_at: datetime,
    actor: str,
    reason: str,
    policy_version: str,
    confirmed: bool,
) -> ExecutionControlState:
    """Enable capital writes without approving a plan or autonomous policy."""
    if not confirmed:
        raise ValueError("Execution enablement requires explicit confirmation.")
    if not actor.strip():
        raise ValueError("Execution enablement requires an actor.")
    if not reason.strip():
        raise ValueError("Execution enablement requires a reason.")
    state: ExecutionControlState = ExecutionControlState(
        disabled=False,
        changed_at=changed_at,
        actor=actor,
        reason=reason,
        policy_version=policy_version,
    )
    kill_switch.enable(state)
    return state
