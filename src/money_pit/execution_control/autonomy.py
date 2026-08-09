"""Module containing staged autonomous-execution eligibility checks."""

from collections.abc import Callable
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.execution_control.models import PreflightDenial
from money_pit.execution_control.models import PreflightDenialCode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan


_MINIMUM_SHADOW_TRADING_DAYS: int = 60
_MINIMUM_SHADOW_PLANS: int = 30
_MINIMUM_PAPER_EXECUTIONS: int = 30
_MINIMUM_APPROVED_LIVE_EXECUTIONS: int = 30


class AutonomyReadinessEvidence(BaseModel):
    """Audited staged-operation evidence required before autonomy."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    as_of: AwareDatetime
    validation_period_start: AwareDatetime
    evidence_event_ids: tuple[str, ...] = Field(min_length=1)
    shadow_trading_days: int = Field(ge=0)
    executable_shadow_plans: int = Field(ge=0)
    approved_paper_executions: int = Field(ge=0)
    approval_required_live_executions: int = Field(ge=0)
    critical_control_failures: int = Field(ge=0)


class AutonomousExecutionPolicy(BaseModel):
    """An explicit versioned opt-in to a bounded autonomous scope."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(min_length=1)
    enabled: bool = False
    allowed_asset_classes: tuple[str, ...] = ("us_equity",)
    long_only: bool = True
    maximum_order_notional: float = Field(gt=0)
    maximum_daily_turnover: float = Field(ge=0, le=1)


class AutonomousActionCoverage(BaseModel):
    """Current deterministic coverage facts for the exact plan."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    asset_classes: dict[str, str]
    liquid_instruments: frozenset[str]
    short_opening_instruments: frozenset[str] = frozenset()


class StagedAutonomyEvaluator:
    """Evaluate readiness and plan scope against explicit autonomous policy."""

    def __init__(
        self,
        *,
        readiness: AutonomyReadinessEvidence,
        autonomous_policy: AutonomousExecutionPolicy,
        coverage: AutonomousActionCoverage,
        verify_durable_evidence: Callable[[AutonomyReadinessEvidence], bool],
    ) -> None:
        """Bind immutable qualification evidence to one evaluation boundary."""
        self._readiness: AutonomyReadinessEvidence = readiness
        self._autonomous_policy: AutonomousExecutionPolicy = autonomous_policy
        self._coverage: AutonomousActionCoverage = coverage
        self._verify_durable_evidence: Callable[[AutonomyReadinessEvidence], bool] = verify_durable_evidence

    def denials(self, plan: PortfolioPlan, policy: ExecutionPolicy) -> tuple[PreflightDenial, ...]:  # noqa: C901
        """Return all unmet readiness, policy, liquidity, and long-only requirements."""
        details: list[str] = []
        autonomy: AutonomousExecutionPolicy = self._autonomous_policy
        readiness: AutonomyReadinessEvidence = self._readiness
        if not self._verify_durable_evidence(readiness):
            details.append("Autonomous readiness evidence is not backed by durable validation records.")
        if readiness.validation_period_start >= readiness.as_of:
            details.append("Autonomous readiness validation period is empty or reversed.")
        if not autonomy.enabled:
            details.append("The versioned autonomous policy is not explicitly enabled.")
        if autonomy.policy_version != policy.policy_version:
            details.append("The autonomous policy version differs from the active execution policy.")
        if autonomy.maximum_order_notional >= policy.maximum_order_notional:
            details.append("Autonomous maximum order notional must be tighter than the base execution policy.")
        if autonomy.maximum_daily_turnover >= policy.maximum_daily_turnover:
            details.append("Autonomous maximum daily turnover must be tighter than the base execution policy.")
        if any(trade.estimated_notional > autonomy.maximum_order_notional for trade in plan.payload.proposed_trades):
            details.append("A proposed trade exceeds the autonomous order-notional cap.")
        if plan.payload.turnover_estimate > autonomy.maximum_daily_turnover:
            details.append("The plan exceeds the autonomous turnover cap.")
        thresholds: tuple[tuple[int, int, str], ...] = (
            (readiness.shadow_trading_days, _MINIMUM_SHADOW_TRADING_DAYS, "shadow trading days"),
            (readiness.executable_shadow_plans, _MINIMUM_SHADOW_PLANS, "executable shadow plans"),
            (readiness.approved_paper_executions, _MINIMUM_PAPER_EXECUTIONS, "approved paper executions"),
            (
                readiness.approval_required_live_executions,
                _MINIMUM_APPROVED_LIVE_EXECUTIONS,
                "approval-required live executions",
            ),
        )
        details.extend(
            f"Autonomy requires at least {minimum} {label}; observed {actual}."
            for actual, minimum, label in thresholds
            if actual < minimum
        )
        if readiness.critical_control_failures != 0:
            details.append("Autonomy requires zero critical control failures in the validation period.")

        instruments: set[str] = {trade.instrument for trade in plan.payload.proposed_trades}
        missing_classes: set[str] = instruments - self._coverage.asset_classes.keys()
        if missing_classes:
            details.append(f"Asset-class coverage is missing for {sorted(missing_classes)!r}.")
        disallowed: set[str] = {
            instrument
            for instrument in instruments - missing_classes
            if self._coverage.asset_classes[instrument] not in autonomy.allowed_asset_classes
        }
        if disallowed:
            details.append(f"Autonomous policy does not cover asset classes for {sorted(disallowed)!r}.")
        illiquid: set[str] = instruments - self._coverage.liquid_instruments
        if illiquid:
            details.append(f"Current liquidity coverage is absent for {sorted(illiquid)!r}.")
        if autonomy.long_only and self._coverage.short_opening_instruments:
            details.append(
                f"Long-only autonomy cannot open short exposure in {sorted(self._coverage.short_opening_instruments)!r}."
            )
        return tuple(
            PreflightDenial(code=PreflightDenialCode.AUTONOMOUS_POLICY_NOT_COVERED, detail=detail) for detail in details
        )
