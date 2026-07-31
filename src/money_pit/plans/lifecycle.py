"""Module containing immutable decisions and portfolio-plan authorization."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.plans.errors import ExecutionDisabledError
from money_pit.plans.errors import PlanApprovalRequiredError
from money_pit.plans.errors import PlanExpiredError
from money_pit.plans.errors import PlanGateFailedError
from money_pit.plans.errors import PlanHashMismatchError
from money_pit.plans.errors import PlanRejectedError
from money_pit.plans.errors import PlanStateDriftError
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan  # noqa: TC001
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload  # noqa: TC001


if TYPE_CHECKING:
    from datetime import datetime


class AuthorizationBasis(StrEnum):
    """Authority under which a plan may be sent to execution."""

    OPERATOR_APPROVAL = "operator_approval"
    AUTONOMOUS_POLICY = "autonomous_policy"


class ApprovalRecord(BaseModel):
    """Immutable operator approval of one exact plan digest."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_at: AwareDatetime
    decided_by: str = Field(min_length=1)


class RejectionRecord(BaseModel):
    """Immutable operator rejection of one exact plan digest."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_at: AwareDatetime
    decided_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


PlanDecision = ApprovalRecord | RejectionRecord


class AuthorizationContext(BaseModel):
    """Current trusted state against which a plan is revalidated."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    evaluated_at: AwareDatetime
    portfolio_snapshot_id: str = Field(min_length=1)
    market_snapshot_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    evidence_gate_results: dict[str, bool]
    constraint_results: dict[str, bool]
    execution_enabled: bool
    asset_classes: dict[str, str]
    committed_turnover: float = Field(ge=0, le=1)


class ExecutionAuthorization(BaseModel):
    """Immutable proof that one exact plan passed pre-execution gates."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_at: AwareDatetime
    portfolio_snapshot_id: str = Field(min_length=1)
    market_snapshot_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    basis: AuthorizationBasis
    approval_decision_id: str | None


def recompute_plan_hash(payload: PortfolioPlanPayload) -> str:
    """Recompute the sole canonical digest from the plan payload."""
    return hashlib.sha256(payload.canonical_bytes()).hexdigest()


def _require_plan_integrity(plan: PortfolioPlan) -> None:
    recomputed_hash: str = recompute_plan_hash(plan.payload)
    if recomputed_hash != plan.plan_hash:
        raise PlanHashMismatchError("plan payload does not match its persisted hash")


def approve_plan(
    plan: PortfolioPlan,
    *,
    decision_id: str,
    decided_at: datetime,
    decided_by: str,
) -> ApprovalRecord:
    """Create an approval bound to the plan's canonical digest."""
    _require_plan_integrity(plan)
    return ApprovalRecord(
        decision_id=decision_id,
        plan_id=plan.payload.plan_id,
        plan_hash=plan.plan_hash,
        decided_at=decided_at,
        decided_by=decided_by,
    )


def reject_plan(
    plan: PortfolioPlan,
    *,
    decision_id: str,
    decided_at: datetime,
    decided_by: str,
    reason: str,
) -> RejectionRecord:
    """Create a rejection bound to the plan's canonical digest."""
    _require_plan_integrity(plan)
    return RejectionRecord(
        decision_id=decision_id,
        plan_id=plan.payload.plan_id,
        plan_hash=plan.plan_hash,
        decided_at=decided_at,
        decided_by=decided_by,
        reason=reason,
    )


def _require_current_state(plan: PortfolioPlan, context: AuthorizationContext) -> None:
    payload: PortfolioPlanPayload = plan.payload
    drifted_fields: list[str] = []
    if context.portfolio_snapshot_id != payload.portfolio_snapshot_id:
        drifted_fields.append("portfolio_snapshot_id")
    if context.market_snapshot_id != payload.market_snapshot_id:
        drifted_fields.append("market_snapshot_id")
    if context.policy_version != payload.policy_version:
        drifted_fields.append("policy_version")
    if context.evidence_gate_results != payload.evidence_gate_results:
        drifted_fields.append("evidence_gate_results")
    if context.constraint_results != payload.constraint_results:
        drifted_fields.append("constraint_results")
    if drifted_fields:
        raise PlanStateDriftError(f"plan-bound state has drifted: {', '.join(drifted_fields)}")


def _require_passing_gates(plan: PortfolioPlan) -> None:
    if not plan.payload.evidence_gate_results:
        raise PlanGateFailedError("the plan has no evidence-gate results")
    if not all(plan.payload.evidence_gate_results.values()):
        raise PlanGateFailedError("one or more evidence gates failed")
    if not plan.payload.constraint_results:
        raise PlanGateFailedError("the plan has no constraint results")
    if not all(plan.payload.constraint_results.values()):
        raise PlanGateFailedError("one or more deterministic constraints failed")


def _require_exact_approval(
    plan: PortfolioPlan,
    decision: PlanDecision | None,
    evaluated_at: datetime,
) -> ApprovalRecord:
    if decision is None:
        raise PlanApprovalRequiredError("this plan requires operator approval")
    if isinstance(decision, RejectionRecord):
        raise PlanRejectedError("this plan was rejected")
    if decision.plan_id != plan.payload.plan_id or decision.plan_hash != plan.plan_hash:
        raise PlanApprovalRequiredError("approval does not match this exact plan")
    if decision.decided_at > plan.payload.expires_at:
        raise PlanApprovalRequiredError("approval was recorded after plan expiry")
    if decision.decided_at < plan.payload.created_at:
        raise PlanApprovalRequiredError("approval predates plan creation")
    if decision.decided_at > evaluated_at:
        raise PlanApprovalRequiredError("approval postdates the authorization evaluation")
    return decision


def _require_autonomous_eligibility(plan: PortfolioPlan) -> None:
    unknown_tax_sells: list[str] = [
        trade.instrument for trade in plan.payload.proposed_trades if trade.side == "sell" and not trade.tax_cost_known
    ]
    if unknown_tax_sells:
        raise PlanGateFailedError(f"autonomous execution cannot make tax-unknown sells: {sorted(unknown_tax_sells)}")


def _require_execution_policy(
    plan: PortfolioPlan,
    execution_policy: ExecutionPolicy,
    context: AuthorizationContext,
) -> None:
    missing_asset_classes: list[str] = [
        trade.instrument for trade in plan.payload.proposed_trades if trade.instrument not in context.asset_classes
    ]
    if missing_asset_classes:
        raise PlanGateFailedError(f"missing asset-class decisions: {sorted(missing_asset_classes)}")
    disallowed_assets: list[str] = [
        trade.instrument
        for trade in plan.payload.proposed_trades
        if context.asset_classes[trade.instrument] not in execution_policy.allowed_asset_classes
    ]
    if disallowed_assets:
        raise PlanGateFailedError(f"disallowed asset classes: {sorted(disallowed_assets)}")
    if any(
        trade.estimated_notional > execution_policy.maximum_order_notional for trade in plan.payload.proposed_trades
    ):
        raise PlanGateFailedError("a proposed trade exceeds maximum order notional")
    if context.committed_turnover + plan.payload.turnover_estimate > execution_policy.maximum_daily_turnover:
        raise PlanGateFailedError("the plan exceeds maximum daily turnover")


def authorize_plan(
    plan: PortfolioPlan,
    *,
    execution_policy: ExecutionPolicy,
    context: AuthorizationContext,
    decision: PlanDecision | None = None,
) -> ExecutionAuthorization:
    """Authorize one exact plan after expiry, state, and authority checks."""
    _require_plan_integrity(plan)
    if not context.execution_enabled:
        raise ExecutionDisabledError("execution is disabled by the global kill switch")
    if execution_policy.execution_mode is ExecutionMode.OBSERVE:
        raise ExecutionDisabledError("observe mode does not grant execution authority")
    if context.evaluated_at >= plan.payload.expires_at:
        raise PlanExpiredError("the portfolio plan has expired")
    if execution_policy.policy_version != plan.payload.policy_version:
        raise PlanStateDriftError("execution policy differs from the plan-bound policy")
    _require_current_state(plan, context)
    _require_passing_gates(plan)

    _require_execution_policy(plan, execution_policy, context)
    if execution_policy.execution_mode is ExecutionMode.APPROVAL_REQUIRED:
        approval: ApprovalRecord = _require_exact_approval(plan, decision, context.evaluated_at)
        basis: AuthorizationBasis = AuthorizationBasis.OPERATOR_APPROVAL
        approval_decision_id: str | None = approval.decision_id
    else:
        if decision is not None and isinstance(decision, RejectionRecord):
            raise PlanRejectedError("this plan was rejected")
        _require_autonomous_eligibility(plan)
        basis = AuthorizationBasis.AUTONOMOUS_POLICY
        approval_decision_id = None

    return ExecutionAuthorization(
        plan_id=plan.payload.plan_id,
        plan_hash=plan.plan_hash,
        authorized_at=context.evaluated_at,
        portfolio_snapshot_id=context.portfolio_snapshot_id,
        market_snapshot_id=context.market_snapshot_id,
        policy_version=context.policy_version,
        basis=basis,
        approval_decision_id=approval_decision_id,
    )
