"""Module containing deterministic portfolio-plan pre-execution validation."""

from collections.abc import Callable
from datetime import datetime
from math import isclose

from money_pit.execution_control.models import PreflightDenial
from money_pit.execution_control.models import PreflightDenialCode
from money_pit.execution_control.models import PreflightResult
from money_pit.execution_control.protocols import AutonomousEligibilityEvaluator
from money_pit.execution_control.protocols import DecisionRepository
from money_pit.execution_control.protocols import KillSwitchStore
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import PlanDecision
from money_pit.plans.lifecycle import RejectionRecord
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import ProposedTrade


DriftCheck = Callable[[PortfolioPlan], bool]


def _denial(code: PreflightDenialCode, detail: str) -> PreflightDenial:
    return PreflightDenial(code=code, detail=detail)


def _approval_binding_denials(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    decision: ApprovalRecord,
    execution_config_hash: str,
) -> list[PreflightDenial]:
    mismatches: tuple[tuple[bool, PreflightDenialCode, str], ...] = (
        (
            decision.execution_config_hash != execution_config_hash,
            PreflightDenialCode.EXECUTION_CONFIG_MISMATCH,
            "The approval covers a different execution configuration.",
        ),
        (
            decision.execution_policy_hash != policy.fingerprint(),
            PreflightDenialCode.EXECUTION_POLICY_MISMATCH,
            "The approval covers a different execution policy.",
        ),
        (
            decision.execution_policy_version != policy.policy_version,
            PreflightDenialCode.POLICY_VERSION_MISMATCH,
            "The approval covers a different policy version.",
        ),
        (
            decision.broker_environment is not policy.broker_environment,
            PreflightDenialCode.BROKER_ENVIRONMENT_MISMATCH,
            "The approval covers a different broker environment.",
        ),
        (
            decision.account_id != plan.payload.account_id,
            PreflightDenialCode.BROKER_ACCOUNT_MISMATCH,
            "The approval covers a different broker account.",
        ),
    )
    return [_denial(code, detail) for failed, code, detail in mismatches if failed]


def _approval_denials(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    decisions: DecisionRepository,
    now: datetime,
    execution_config_hash: str,
    autonomous_eligibility: AutonomousEligibilityEvaluator | None = None,
) -> list[PreflightDenial]:
    rejection = decisions.rejection_for(plan.payload.plan_id, plan.plan_hash)
    if rejection is not None:
        return [_denial(PreflightDenialCode.PLAN_REJECTED, "The exact plan hash was rejected.")]
    decision = decisions.latest_for(plan.payload.plan_id)
    if (
        isinstance(decision, RejectionRecord)
        and decision.plan_id == plan.payload.plan_id
        and decision.plan_hash == plan.plan_hash
    ):
        return [_denial(PreflightDenialCode.PLAN_REJECTED, "The exact plan hash was rejected.")]
    mode_denials = _execution_mode_denials(plan, policy, autonomous_eligibility)
    if mode_denials is not None:
        return mode_denials
    if decision is None:
        return [_denial(PreflightDenialCode.APPROVAL_MISSING, "No durable operator decision exists.")]
    return _operator_decision_denials(plan, policy, decision, now, execution_config_hash)


def _execution_mode_denials(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    autonomous_eligibility: AutonomousEligibilityEvaluator | None,
) -> list[PreflightDenial] | None:
    if policy.execution_mode is ExecutionMode.OBSERVE:
        return [_denial(PreflightDenialCode.OBSERVE_ONLY, "Execution policy permits observation only.")]
    if policy.execution_mode is ExecutionMode.AUTONOMOUS:
        if autonomous_eligibility is None:
            return [
                _denial(
                    PreflightDenialCode.AUTONOMOUS_POLICY_NOT_COVERED,
                    "No versioned autonomous eligibility evaluator is installed.",
                )
            ]
        return list(autonomous_eligibility.denials(plan, policy))
    if policy.execution_mode is not ExecutionMode.APPROVAL_REQUIRED:
        raise AssertionError(f"Unhandled execution mode: {policy.execution_mode!r}")
    return None


def _operator_decision_denials(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    decision: PlanDecision,
    now: datetime,
    execution_config_hash: str,
) -> list[PreflightDenial]:
    if decision.plan_id != plan.payload.plan_id or decision.plan_hash != plan.plan_hash:
        return [
            _denial(
                PreflightDenialCode.APPROVAL_HASH_MISMATCH,
                "The latest operator decision covers a different plan hash.",
            )
        ]
    if isinstance(decision, RejectionRecord):
        return [_denial(PreflightDenialCode.PLAN_REJECTED, "The exact plan hash was rejected.")]
    if decision.decided_at < plan.payload.created_at or decision.decided_at > plan.payload.expires_at:
        return [
            _denial(
                PreflightDenialCode.APPROVAL_HASH_MISMATCH,
                "The exact-plan approval was recorded outside the plan's valid interval.",
            )
        ]
    mismatches = _approval_binding_denials(plan, policy, decision, execution_config_hash)
    if mismatches:
        return mismatches
    if decision.decided_at > now:
        return [
            _denial(
                PreflightDenialCode.APPROVAL_HASH_MISMATCH,
                "The exact-plan approval is future-dated.",
            )
        ]
    return []


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
    except Exception as error:  # noqa: BLE001 - normalize every provider/storage boundary failure.
        return _denial(code, f"{label} state is unavailable: {type(error).__name__}.")
    if unchanged:
        return None
    return _denial(code, f"The current {label} state differs from the plan snapshot.")


def _plan_contract_denials(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    now: datetime,
    committed_turnover: float,
    execution_config_hash: str,
) -> list[PreflightDenial]:
    """Return denials intrinsic to the immutable plan and active policy."""
    denials: list[PreflightDenial] = []
    if plan.payload.sha256() != plan.plan_hash:
        denials.append(
            _denial(
                PreflightDenialCode.PLAN_HASH_MISMATCH, "The persisted hash does not match the canonical plan payload."
            )
        )
    if now >= plan.payload.expires_at:
        denials.append(_denial(PreflightDenialCode.PLAN_EXPIRED, "The portfolio plan has expired."))
    if plan.payload.historical_non_executable:
        denials.append(
            _denial(PreflightDenialCode.HISTORICAL_PLAN, "Historical point-in-time plans are not executable.")
        )
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
    denials.extend(
        _denial(
            PreflightDenialCode.CONSTRAINT_FAILED,
            f"The execution policy does not permit {trade.asset_class.value} for {trade.instrument}.",
        )
        for trade in plan.payload.proposed_trades
        if trade.asset_class not in policy.allowed_asset_classes
    )
    denials.extend(_execution_binding_denials(plan, policy, execution_config_hash))
    if plan.payload.turnover_estimate > policy.maximum_daily_turnover:
        denials.append(
            _denial(PreflightDenialCode.CONSTRAINT_FAILED, "Plan turnover exceeds the execution-policy daily limit.")
        )
    if committed_turnover + plan.payload.turnover_estimate > policy.maximum_daily_turnover:
        denials.append(
            _denial(
                PreflightDenialCode.CONSTRAINT_FAILED, "Cumulative daily turnover exceeds the execution-policy limit."
            )
        )
    return denials


def _execution_binding_denials(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    execution_config_hash: str,
) -> list[PreflightDenial]:
    checks: tuple[tuple[bool, PreflightDenialCode, str], ...] = (
        (
            plan.payload.broker_environment is not policy.broker_environment,
            PreflightDenialCode.BROKER_ENVIRONMENT_MISMATCH,
            "The plan and active policy target different broker environments.",
        ),
        (
            policy.execution_mode is ExecutionMode.AUTONOMOUS and plan.payload.execution_policy_hash is None,
            PreflightDenialCode.EXECUTION_BINDING_MISSING,
            "Autonomous execution requires a plan-bound execution policy.",
        ),
        (
            plan.payload.execution_config_hash is not None
            and plan.payload.execution_config_hash != execution_config_hash,
            PreflightDenialCode.EXECUTION_CONFIG_MISMATCH,
            "The plan binds a different execution configuration.",
        ),
        (
            plan.payload.execution_policy_hash is not None
            and plan.payload.execution_policy_hash != policy.fingerprint(),
            PreflightDenialCode.EXECUTION_POLICY_MISMATCH,
            "The plan binds a different execution policy.",
        ),
    )
    return [_denial(code, detail) for failed, code, detail in checks if failed]


def validate_pre_execution(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    decisions: DecisionRepository,
    kill_switch: KillSwitchStore,
    *,
    now: datetime,
    portfolio_unchanged: DriftCheck,
    market_unchanged: DriftCheck,
    execution_config_hash: str,
    committed_turnover: float = 0.0,
    autonomous_eligibility: AutonomousEligibilityEvaluator | None = None,
) -> PreflightResult:
    """Evaluate every deterministic authority and freshness gate without short-circuiting."""
    denials: list[PreflightDenial] = []
    control_state = kill_switch.get_control_state()
    if control_state.disabled:
        denials.append(
            _denial(
                PreflightDenialCode.KILL_SWITCH_DISABLED,
                "The global execution kill switch is disabled.",
            )
        )
    if control_state.policy_version != policy.policy_version:
        denials.append(
            _denial(
                PreflightDenialCode.POLICY_VERSION_MISMATCH,
                "The execution-control enablement covers a different policy version.",
            )
        )
    denials.extend(_plan_contract_denials(plan, policy, now, committed_turnover, execution_config_hash))
    denials.extend(_approval_denials(plan, policy, decisions, now, execution_config_hash, autonomous_eligibility))
    denials.extend(_tax_denials(plan, policy))
    denials.extend(_notional_denials(plan.payload.proposed_trades, policy.maximum_order_notional))
    portfolio_denial = _drift_denial(plan, portfolio_unchanged, PreflightDenialCode.PORTFOLIO_STATE_DRIFT, "portfolio")
    market_denial = _drift_denial(plan, market_unchanged, PreflightDenialCode.MARKET_STATE_DRIFT, "market")
    denials.extend(denial for denial in (portfolio_denial, market_denial) if denial is not None)
    return PreflightResult(
        plan_id=plan.payload.plan_id,
        plan_hash=plan.plan_hash,
        checked_at=now,
        denials=tuple(denials),
    )
