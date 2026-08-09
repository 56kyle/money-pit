"""Module containing typed execution-control decisions."""

from datetime import date
from datetime import datetime
from enum import StrEnum
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class ApprovalDecision(StrEnum):
    """An operator's immutable decision for one exact plan hash."""

    APPROVED = "approved"
    REJECTED = "rejected"


class ExecutionControlState(BaseModel):
    """The durable global execution kill-switch state."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    disabled: bool
    changed_at: AwareDatetime
    actor: str = Field(min_length=1)
    reason: str | None = None
    policy_version: str = Field(min_length=1)


class PreflightDenialCode(StrEnum):
    """A stable reason that prevents portfolio-plan execution."""

    OBSERVE_ONLY = "observe_only"
    AUTONOMOUS_POLICY_NOT_COVERED = "autonomous_policy_not_covered"
    KILL_SWITCH_DISABLED = "kill_switch_disabled"
    PLAN_HASH_MISMATCH = "plan_hash_mismatch"
    PLAN_EXPIRED = "plan_expired"
    HISTORICAL_PLAN = "historical_plan"
    POLICY_VERSION_MISMATCH = "policy_version_mismatch"
    EXECUTION_BINDING_MISSING = "execution_binding_missing"
    EXECUTION_CONFIG_MISMATCH = "execution_config_mismatch"
    EXECUTION_POLICY_MISMATCH = "execution_policy_mismatch"
    BROKER_ENVIRONMENT_MISMATCH = "broker_environment_mismatch"
    BROKER_ACCOUNT_MISMATCH = "broker_account_mismatch"
    BROKER_ACCOUNT_BLOCKED = "broker_account_blocked"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_HASH_MISMATCH = "approval_hash_mismatch"
    PLAN_REJECTED = "plan_rejected"
    EVIDENCE_GATE_FAILED = "evidence_gate_failed"
    CONSTRAINT_FAILED = "constraint_failed"
    TAX_COST_UNKNOWN = "tax_cost_unknown"
    ORDER_NOTIONAL_EXCEEDED = "order_notional_exceeded"
    PORTFOLIO_STATE_DRIFT = "portfolio_state_drift"
    MARKET_STATE_DRIFT = "market_state_drift"
    CASH_STATE_DRIFT = "cash_state_drift"
    OPEN_ORDERS_STATE_DRIFT = "open_orders_state_drift"
    EVIDENCE_STALE = "evidence_stale"
    TAX_STATE_DRIFT = "tax_state_drift"
    CURRENT_STATE_UNAVAILABLE = "current_state_unavailable"


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


def enabled_execution_state(
    now: datetime,
    *,
    policy_version: str,
    actor: str = "system",
) -> ExecutionControlState:
    """Return the initial enabled kill-switch state."""
    return ExecutionControlState(
        disabled=False,
        changed_at=now,
        actor=actor,
        policy_version=policy_version,
    )


class ExecutionEventPhase(StrEnum):
    """One durable phase in a plan trade's execution lifecycle."""

    INTENT_RECORDED = "intent_recorded"
    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_OBSERVED = "recovery_observed"
    RECOVERY_HALTED = "recovery_halted"


class ExecutionEvent(BaseModel):
    """An immutable execution or recovery journal event."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    trade_identity: str | None = None
    client_order_id: str | None = None
    phase: ExecutionEventPhase
    occurred_at: AwareDatetime
    broker_order_id: str | None = None
    detail: dict[str, object] = Field(default_factory=dict)


class ExecutionClaim(BaseModel):
    """The durable latest state of one idempotently claimed trade."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    trade_identity: str = Field(min_length=1)
    status: str = Field(min_length=1)
    claimed_at: AwareDatetime
    updated_at: AwareDatetime
    broker_order_id: str | None = None


class ExecutionReceipt(BaseModel):
    """The completed or interrupted result of executing one exact plan."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    started_at: AwareDatetime
    finished_at: AwareDatetime
    submitted_trade_identities: tuple[str, ...]
    completed_trade_identities: tuple[str, ...]
    halted: bool


class TurnoverReservationStatus(StrEnum):
    """Lifecycle state of capital committed against one trading-day cap."""

    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"


class TurnoverReservation(BaseModel):
    """Concurrent-safe reservation against one broker account's daily turnover cap."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    reservation_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    trading_date: date
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    amount_fraction: float = Field(gt=0, le=1)
    maximum_fraction: float = Field(gt=0, le=1)
    status: TurnoverReservationStatus
    reserved_at: AwareDatetime
    terminal_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        """Require cap consistency and status-specific terminal timing."""
        if self.amount_fraction > self.maximum_fraction:
            raise ValueError("amount_fraction cannot exceed maximum_fraction")
        if (self.status is TurnoverReservationStatus.RESERVED) != (self.terminal_at is None):
            raise ValueError("terminal_at must be absent exactly while turnover is reserved")
        if self.terminal_at is not None and self.terminal_at < self.reserved_at:
            raise ValueError("terminal_at cannot precede reserved_at")
        return self


class ConfirmedFill(BaseModel):
    """A terminal fill applied to expected state before the next-leg revalidation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    trade_identity: str = Field(min_length=1)
    instrument: str = Field(min_length=1)
    side: str = Field(pattern=r"^(buy|sell)$")
    filled_qty: float = Field(gt=0)
    filled_avg_price: float = Field(gt=0)
    realized_notional: float = Field(gt=0)


class RecoveryResult(BaseModel):
    """The durable reconciliation result for all nonterminal claims."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    checked_at: AwareDatetime
    reconciled_trade_identities: tuple[str, ...]
    unresolved_trade_identities: tuple[str, ...]

    @property
    def allows_new_execution(self) -> bool:
        """Return whether no prior claim can overlap a new plan."""
        return not self.unresolved_trade_identities
