"""Module containing typed execution-control decisions."""

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class ApprovalDecision(StrEnum):
    """Compatibility decision enum for execution-control callers."""

    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRecord(BaseModel):
    """Compatibility decision record for preflight protocol callers."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decision: ApprovalDecision
    decided_at: AwareDatetime
    actor: str = Field(min_length=1)
    reason: str | None = None


class ExecutionControlState(BaseModel):
    """The durable global execution kill-switch state."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    disabled: bool
    changed_at: AwareDatetime
    actor: str = Field(min_length=1)
    reason: str | None = None


class PreflightDenialCode(StrEnum):
    """A stable reason that prevents portfolio-plan execution."""

    OBSERVE_ONLY = "observe_only"
    AUTONOMOUS_POLICY_NOT_COVERED = "autonomous_policy_not_covered"
    KILL_SWITCH_DISABLED = "kill_switch_disabled"
    PLAN_HASH_MISMATCH = "plan_hash_mismatch"
    PLAN_EXPIRED = "plan_expired"
    POLICY_VERSION_MISMATCH = "policy_version_mismatch"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_HASH_MISMATCH = "approval_hash_mismatch"
    PLAN_REJECTED = "plan_rejected"
    EVIDENCE_GATE_FAILED = "evidence_gate_failed"
    CONSTRAINT_FAILED = "constraint_failed"
    TAX_COST_UNKNOWN = "tax_cost_unknown"
    ORDER_NOTIONAL_EXCEEDED = "order_notional_exceeded"
    PORTFOLIO_STATE_DRIFT = "portfolio_state_drift"
    MARKET_STATE_DRIFT = "market_state_drift"


class PreflightDenial(BaseModel):
    """One typed fail-closed pre-execution finding."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    code: PreflightDenialCode
    detail: str = Field(min_length=1)


class PreflightResult(BaseModel):
    """The exhaustive result of evaluating all plan execution gates."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    checked_at: AwareDatetime
    denials: tuple[PreflightDenial, ...]

    @property
    def allowed(self) -> bool:
        """Return whether every pre-execution gate passed."""
        return not self.denials


def enabled_execution_state(now: datetime, *, actor: str = "system") -> ExecutionControlState:
    """Return the initial enabled kill-switch state."""
    return ExecutionControlState(disabled=False, changed_at=now, actor=actor)
