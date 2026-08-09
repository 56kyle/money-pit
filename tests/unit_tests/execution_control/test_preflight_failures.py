"""Exhaustive fail-closed tests for pre-execution authority gates."""

from dataclasses import dataclass
from datetime import datetime

import pytest

from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.models import PreflightDenialCode
from money_pit.execution_control.preflight import validate_pre_execution
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import PlanDecision
from money_pit.plans.lifecycle import RejectionRecord
from money_pit.plans.lifecycle import approve_plan
from money_pit.plans.lifecycle import reject_plan
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.portfolio_plan import PortfolioPlan


@dataclass(frozen=True)
class _DecisionStore:
    decision: PlanDecision | None
    rejection: RejectionRecord | None = None

    def append(self, record: PlanDecision) -> None:
        raise AssertionError(record)

    def latest_for(self, plan_id: str) -> PlanDecision | None:
        del plan_id
        return self.decision

    def rejection_for(self, plan_id: str, plan_hash: str) -> RejectionRecord | None:
        if self.rejection is None:
            return None
        return self.rejection if (self.rejection.plan_id, self.rejection.plan_hash) == (plan_id, plan_hash) else None


def _policy(mode: ExecutionMode = ExecutionMode.APPROVAL_REQUIRED) -> ExecutionPolicy:
    return ExecutionPolicy(
        policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=mode,
        maximum_order_notional=1_000.0,
        maximum_daily_turnover=0.2,
    )


def _unchanged(_plan: PortfolioPlan) -> bool:
    return True


def _changed(_plan: PortfolioPlan) -> bool:
    return False


def _enable(repository: SqliteExecutionAuthorityRepository, now: datetime) -> None:
    repository.enable(
        ExecutionControlState(
            disabled=False,
            changed_at=now,
            actor="operator",
            policy_version="policy-1",
        ),
    )


def _validate(
    repository: SqliteExecutionAuthorityRepository,
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    now: datetime,
    *,
    decision: PlanDecision | None = None,
    rejection: RejectionRecord | None = None,
    portfolio_unchanged: bool = True,
    market_unchanged: bool = True,
) -> set[PreflightDenialCode]:
    result = validate_pre_execution(
        plan,
        policy,
        _DecisionStore(decision, rejection),
        repository,
        now=now,
        portfolio_unchanged=_unchanged if portfolio_unchanged else _changed,
        market_unchanged=_unchanged if market_unchanged else _changed,
        execution_config_hash="e" * 64,
    )
    return {denial.code for denial in result.denials}


def test_validate_pre_execution_with_exact_approval_allows_plan(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    _enable(execution_repository, now)
    decision = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
        execution_config_hash="e" * 64,
        execution_policy=_policy(),
        account_id=portfolio_plan.payload.account_id,
        committed_turnover_at_approval=0.0,
    )

    codes = _validate(execution_repository, portfolio_plan, _policy(), now, decision=decision)

    assert not codes


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        pytest.param("observe", PreflightDenialCode.OBSERVE_ONLY, id="observe"),
        pytest.param("tampered", PreflightDenialCode.PLAN_HASH_MISMATCH, id="tampered"),
        pytest.param("expired", PreflightDenialCode.PLAN_EXPIRED, id="expired"),
        pytest.param("policy", PreflightDenialCode.POLICY_VERSION_MISMATCH, id="policy"),
        pytest.param("empty-constraints", PreflightDenialCode.CONSTRAINT_FAILED, id="empty-constraints"),
        pytest.param("failed-evidence", PreflightDenialCode.EVIDENCE_GATE_FAILED, id="failed-evidence"),
        pytest.param("failed-constraint", PreflightDenialCode.CONSTRAINT_FAILED, id="failed-constraint"),
        pytest.param("notional", PreflightDenialCode.ORDER_NOTIONAL_EXCEEDED, id="notional"),
        pytest.param("asset-class", PreflightDenialCode.CONSTRAINT_FAILED, id="asset-class"),
        pytest.param("turnover", PreflightDenialCode.CONSTRAINT_FAILED, id="turnover"),
        pytest.param("portfolio-drift", PreflightDenialCode.PORTFOLIO_STATE_DRIFT, id="portfolio-drift"),
        pytest.param("market-drift", PreflightDenialCode.MARKET_STATE_DRIFT, id="market-drift"),
    ],
)
def test_validate_pre_execution_reports_each_failed_gate(  # noqa: C901
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
    case: str,
    expected_code: PreflightDenialCode,
) -> None:
    _enable(execution_repository, now)
    plan = portfolio_plan
    policy = _policy()
    portfolio_unchanged = True
    market_unchanged = True
    if case == "observe":
        policy = _policy(ExecutionMode.OBSERVE)
    elif case == "tampered":
        plan = PortfolioPlan.model_construct(payload=plan.payload, plan_hash="0" * 64)
    elif case == "expired":
        plan = PortfolioPlan.from_payload(plan.payload.model_copy(update={"expires_at": now}))
    elif case == "policy":
        policy = policy.model_copy(update={"policy_version": "policy-2"})
    elif case == "empty-constraints":
        plan = PortfolioPlan.from_payload(plan.payload.model_copy(update={"constraint_results": {}}))
    elif case == "failed-evidence":
        plan = PortfolioPlan.from_payload(plan.payload.model_copy(update={"evidence_gate_results": {"support": False}}))
    elif case == "failed-constraint":
        plan = PortfolioPlan.from_payload(plan.payload.model_copy(update={"constraint_results": {"position": False}}))
    elif case == "notional":
        trade = plan.payload.proposed_trades[0].model_copy(update={"estimated_notional": 1_001.0})
        plan = PortfolioPlan.from_payload(plan.payload.model_copy(update={"proposed_trades": (trade,)}))
    elif case == "asset-class":
        policy = policy.model_copy(update={"allowed_asset_classes": (TradableAssetClass.US_ETF,)})
    elif case == "turnover":
        plan = PortfolioPlan.from_payload(plan.payload.model_copy(update={"turnover_estimate": 0.3}))
    elif case == "portfolio-drift":
        portfolio_unchanged = False
    elif case == "market-drift":
        market_unchanged = False
    decision = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
        execution_config_hash="e" * 64,
        execution_policy=_policy(),
        account_id=portfolio_plan.payload.account_id,
        committed_turnover_at_approval=0.0,
    )

    codes = _validate(
        execution_repository,
        plan,
        policy,
        now,
        decision=decision,
        portfolio_unchanged=portfolio_unchanged,
        market_unchanged=market_unchanged,
    )

    assert expected_code in codes


def test_validate_pre_execution_rejects_mismatched_approval_hash(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    _enable(execution_repository, now)
    decision = ApprovalRecord(
        decision_id="decision-1",
        plan_id=portfolio_plan.payload.plan_id,
        plan_hash="0" * 64,
        decided_at=now,
        decided_by="operator",
        execution_config_hash="e" * 64,
        execution_policy_hash=_policy().fingerprint(),
        execution_policy_version=_policy().policy_version,
        broker_environment=_policy().broker_environment,
        account_id=portfolio_plan.payload.account_id,
        committed_turnover_at_approval=0.0,
    )

    codes = _validate(execution_repository, portfolio_plan, _policy(), now, decision=decision)

    assert PreflightDenialCode.APPROVAL_HASH_MISMATCH in codes


def test_validate_pre_execution_respects_exact_rejection(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    _enable(execution_repository, now)
    decision = reject_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
        reason="risk changed",
    )

    codes = _validate(execution_repository, portfolio_plan, _policy(), now, decision=decision)

    assert PreflightDenialCode.PLAN_REJECTED in codes


def test_validate_pre_execution_keeps_exact_rejection_as_terminal_veto_after_later_approval(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    _enable(execution_repository, now)
    rejection = reject_plan(
        portfolio_plan,
        decision_id="decision-reject",
        decided_at=now,
        decided_by="operator",
        reason="risk changed",
    )
    later_approval = approve_plan(
        portfolio_plan,
        decision_id="decision-approve",
        decided_at=now,
        decided_by="operator",
        execution_config_hash="e" * 64,
        execution_policy=_policy(),
        account_id=portfolio_plan.payload.account_id,
        committed_turnover_at_approval=0,
    )

    codes = _validate(
        execution_repository,
        portfolio_plan,
        _policy(),
        now,
        decision=later_approval,
        rejection=rejection,
    )

    assert PreflightDenialCode.PLAN_REJECTED in codes
