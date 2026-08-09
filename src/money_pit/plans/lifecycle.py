"""Module containing immutable decisions and portfolio-plan authorization."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.plans.errors import PlanHashMismatchError
from money_pit.schemas.execution_policy import BrokerEnvironment  # noqa: TC001
from money_pit.schemas.execution_policy import ExecutionPolicy  # noqa: TC001
from money_pit.schemas.portfolio_plan import PortfolioPlan  # noqa: TC001
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload  # noqa: TC001


if TYPE_CHECKING:
    from datetime import datetime


class ApprovalRecord(BaseModel):
    """Immutable operator approval of one exact plan digest."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_at: AwareDatetime
    decided_by: str = Field(min_length=1)
    execution_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy_version: str = Field(min_length=1)
    broker_environment: BrokerEnvironment
    account_id: str = Field(min_length=1)
    committed_turnover_at_approval: float = Field(ge=0, le=1)


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
    execution_config_hash: str,
    execution_policy: ExecutionPolicy,
    account_id: str,
    committed_turnover_at_approval: float,
) -> ApprovalRecord:
    """Create an approval bound to the plan's canonical digest."""
    _require_plan_integrity(plan)
    return ApprovalRecord(
        decision_id=decision_id,
        plan_id=plan.payload.plan_id,
        plan_hash=plan.plan_hash,
        decided_at=decided_at,
        decided_by=decided_by,
        execution_config_hash=execution_config_hash,
        execution_policy_hash=execution_policy.fingerprint(),
        execution_policy_version=execution_policy.policy_version,
        broker_environment=execution_policy.broker_environment,
        account_id=account_id,
        committed_turnover_at_approval=committed_turnover_at_approval,
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
