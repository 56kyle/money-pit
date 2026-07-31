"""Module containing deterministic portfolio-plan pre-execution validation."""

from collections.abc import Callable
from datetime import datetime
from math import isclose

from money_pit.execution_control.errors import StateCheckUnavailableError
from money_pit.execution_control.models import PreflightDenial
from money_pit.execution_control.models import PreflightDenialCode
from money_pit.execution_control.models import PreflightResult
from money_pit.execution_control.protocols import DecisionRepository
from money_pit.execution_control.protocols import KillSwitchStore
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import RejectionRecord
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import ProposedTrade


DriftCheck = Callable[[PortfolioPlan], bool]


def _denial(code: PreflightDenialCode, detail: str) -> PreflightDenial:
    return PreflightDenial(code=code, detail=detail)


def _approval_denials(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    decisions: DecisionRepository,
) -> list[PreflightDenial]:
    if policy.execution_mode is ExecutionMode.OBSERVE:
        return [_denial(PreflightDenialCode.OBSERVE_ONLY, "Execution policy permits observation only.")]
    if policy.execution_mode is ExecutionMode.AUTONOMOUS:
        return [
            _denial(
                PreflightDenialCode.AUTONOMOUS_POLICY_NOT_COVERED,
                "No versioned autonomous action-coverage evaluator is installed.",
            )
        ]
    if policy.execution_mode is not ExecutionMode.APPROVAL_REQUIRED:
        raise AssertionError(f"Unhandled execution mode: {policy.execution_mode!r}")
    decision = decisions.latest_for(plan.payload.plan_id)
    if decision is None:
        return [_denial(PreflightDenialCode.APPROVAL_MISSING, "No durable operator decision exists.")]
    if decision.plan_hash != plan.plan_hash:
        return [
            _denial(
                PreflightDenialCode.APPROVAL_HASH_MISMATCH,
                "The latest operator decision covers a different plan hash.",
            )
        ]
    decision_value: object = decision
    if isinstance(decision_value, RejectionRecord):
        return [_denial(PreflightDenialCode.PLAN_REJECTED, "The exact plan hash was rejected.")]
    if isinstance(decision_value, ApprovalRecord):
        return []
    raise AssertionError(f"Unhandled approval decision: {decision_value!r}")


def _tax_denials(plan: PortfolioPlan, policy: ExecutionPolicy) -> list[PreflightDenial]:
    if policy.execution_mode is not ExecutionMode.AUTONOMOUS:
        return []
    return [
        _denial(
            PreflightDenialCode.TAX_COST_UNKNOWN,
            f"Autonomous sell of {trade.instrument} has unknown tax cost.",
        )
        for trade in plan.payload.proposed_trades
        if trade.side == "sell" and not trade.tax_cost_known
    ]


def _notional_denials(trades: tuple[ProposedTrade, ...], maximum_order_notional: float) -> list[PreflightDenial]:
    return [
        _denial(
            PreflightDenialCode.ORDER_NOTIONAL_EXCEEDED,
            f"{trade.instrument} estimated notional exceeds the execution-policy limit.",
        )
        for trade in trades
        if trade.estimated_notional > maximum_order_notional
        and not isclose(trade.estimated_notional, maximum_order_notional)
    ]


def _drift_denial(
    plan: PortfolioPlan,
    check: DriftCheck,
    code: PreflightDenialCode,
    label: str,
) -> PreflightDenial | None:
    """Return a denial for drift or unavailable state instead of propagating ambiguity."""
    try:
        unchanged: bool = check(plan)
    except StateCheckUnavailableError as error:
        return _denial(code, f"{label} state is unavailable: {type(error).__name__}.")
    if unchanged:
        return None
    return _denial(code, f"The current {label} state differs from the plan snapshot.")


def validate_pre_execution(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    decisions: DecisionRepository,
    kill_switch: KillSwitchStore,
    *,
    now: datetime,
    portfolio_unchanged: DriftCheck,
    market_unchanged: DriftCheck,
) -> PreflightResult:
    """Evaluate every deterministic authority and freshness gate without short-circuiting."""
    denials: list[PreflightDenial] = []
    if kill_switch.get_control_state().disabled:
        denials.append(
            _denial(
                PreflightDenialCode.KILL_SWITCH_DISABLED,
                "The global execution kill switch is disabled.",
            )
        )
    recomputed_hash: str = plan.payload.sha256()
    if recomputed_hash != plan.plan_hash:
        denials.append(
            _denial(
                PreflightDenialCode.PLAN_HASH_MISMATCH,
                "The persisted hash does not match the canonical plan payload.",
            )
        )
    if now >= plan.payload.expires_at:
        denials.append(_denial(PreflightDenialCode.PLAN_EXPIRED, "The portfolio plan has expired."))
    if plan.payload.policy_version != policy.policy_version:
        denials.append(
            _denial(
                PreflightDenialCode.POLICY_VERSION_MISMATCH,
                "The plan and active execution policy versions differ.",
            )
        )
    denials.extend(_approval_denials(plan, policy, decisions))
    if not plan.payload.evidence_gate_results:
        denials.append(_denial(PreflightDenialCode.EVIDENCE_GATE_FAILED, "Evidence gate results are missing."))
    if not plan.payload.constraint_results:
        denials.append(_denial(PreflightDenialCode.CONSTRAINT_FAILED, "Constraint results are missing."))
    denials.extend(
        _denial(PreflightDenialCode.EVIDENCE_GATE_FAILED, f"Evidence gate {name!r} failed.")
        for name, passed in sorted(plan.payload.evidence_gate_results.items())
        if not passed
    )
    denials.extend(
        _denial(PreflightDenialCode.CONSTRAINT_FAILED, f"Constraint {name!r} failed.")
        for name, passed in sorted(plan.payload.constraint_results.items())
        if not passed
    )
    denials.extend(_tax_denials(plan, policy))
    denials.extend(_notional_denials(plan.payload.proposed_trades, policy.maximum_order_notional))
    if "us_equity" not in policy.allowed_asset_classes:
        denials.append(
            _denial(
                PreflightDenialCode.CONSTRAINT_FAILED,
                "The execution policy does not permit the plan's US-equity asset class.",
            )
        )
    if plan.payload.turnover_estimate > policy.maximum_daily_turnover:
        denials.append(
            _denial(
                PreflightDenialCode.CONSTRAINT_FAILED,
                "Plan turnover exceeds the execution-policy daily limit.",
            )
        )
    portfolio_denial = _drift_denial(plan, portfolio_unchanged, PreflightDenialCode.PORTFOLIO_STATE_DRIFT, "portfolio")
    market_denial = _drift_denial(plan, market_unchanged, PreflightDenialCode.MARKET_STATE_DRIFT, "market")
    denials.extend(denial for denial in (portfolio_denial, market_denial) if denial is not None)
    return PreflightResult(
        plan_id=plan.payload.plan_id,
        plan_hash=plan.plan_hash,
        checked_at=now,
        denials=tuple(denials),
    )
