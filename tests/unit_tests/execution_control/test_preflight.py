"""Tests for deterministic pre-execution validation."""

from dataclasses import dataclass
from datetime import datetime

import pytest

from money_pit.execution_control.errors import StateCheckUnavailableError
from money_pit.execution_control.gateway import _state_denial  # pyright: ignore[reportPrivateUsage]
from money_pit.execution_control.models import ConfirmedFill
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.models import PreflightDenialCode
from money_pit.execution_control.preflight import validate_pre_execution
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import PlanDecision
from money_pit.plans.lifecycle import RejectionRecord
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload


_EXECUTION_CONFIG_HASH = "a" * 64


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


def _policy(mode: ExecutionMode) -> ExecutionPolicy:
    return ExecutionPolicy(
        policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=mode,
        maximum_order_notional=1_000.0,
        maximum_daily_turnover=0.2,
    )


def _unchanged(_plan: PortfolioPlan) -> bool:
    return True


def _unavailable(_plan: PortfolioPlan) -> bool:
    raise StateCheckUnavailableError("upstream unavailable")


def _unavailable_current_state(
    _plan: PortfolioPlan,
    _confirmed_fills: tuple[ConfirmedFill, ...],
) -> bool:
    raise StateCheckUnavailableError("upstream unavailable")


def _enable(
    repository: SqliteExecutionAuthorityRepository,
    now: datetime,
    *,
    policy_version: str = "policy-1",
) -> None:
    repository.enable(
        ExecutionControlState(
            disabled=False,
            changed_at=now,
            actor="operator",
            policy_version=policy_version,
        )
    )


def test_validate_pre_execution_autonomous_without_coverage_fails_closed(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    result = validate_pre_execution(
        portfolio_plan,
        _policy(ExecutionMode.AUTONOMOUS),
        execution_repository,
        execution_repository,
        now=now,
        portfolio_unchanged=_unchanged,
        market_unchanged=_unchanged,
        execution_config_hash=_EXECUTION_CONFIG_HASH,
    )

    assert PreflightDenialCode.AUTONOMOUS_POLICY_NOT_COVERED in {denial.code for denial in result.denials}


def test_validate_pre_execution_with_missing_evidence_gates_fails_closed(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    payload: PortfolioPlanPayload = portfolio_plan.payload.model_copy(update={"evidence_gate_results": {}})
    plan: PortfolioPlan = PortfolioPlan.from_payload(payload)

    result = validate_pre_execution(
        plan,
        _policy(ExecutionMode.APPROVAL_REQUIRED),
        execution_repository,
        execution_repository,
        now=now,
        portfolio_unchanged=_unchanged,
        market_unchanged=_unchanged,
        execution_config_hash=_EXECUTION_CONFIG_HASH,
    )

    assert PreflightDenialCode.EVIDENCE_GATE_FAILED in {denial.code for denial in result.denials}


def test_validate_pre_execution_with_unavailable_portfolio_state_fails_closed(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    result = validate_pre_execution(
        portfolio_plan,
        _policy(ExecutionMode.APPROVAL_REQUIRED),
        execution_repository,
        execution_repository,
        now=now,
        portfolio_unchanged=_unavailable,
        market_unchanged=_unchanged,
        execution_config_hash=_EXECUTION_CONFIG_HASH,
    )

    assert PreflightDenialCode.PORTFOLIO_STATE_DRIFT in {denial.code for denial in result.denials}


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(PreflightDenialCode.CASH_STATE_DRIFT, id="cash"),
        pytest.param(PreflightDenialCode.OPEN_ORDERS_STATE_DRIFT, id="open-orders"),
        pytest.param(PreflightDenialCode.EVIDENCE_STALE, id="evidence"),
        pytest.param(PreflightDenialCode.TAX_STATE_DRIFT, id="tax"),
        pytest.param(PreflightDenialCode.BROKER_ACCOUNT_BLOCKED, id="broker-account"),
        pytest.param(PreflightDenialCode.MARKET_STATE_DRIFT, id="quote-freshness"),
    ],
)
def test__state_denial_with_unavailable_state_preserves_typed_code(
    portfolio_plan: PortfolioPlan,
    code: PreflightDenialCode,
) -> None:
    denial = _state_denial(
        portfolio_plan,
        (),
        _unavailable_current_state,
        code,
        "current state",
    )

    assert denial is not None
    assert denial.code is code


@pytest.mark.parametrize(
    ("approval_update", "policy_update", "config_hash", "expected_code"),
    [
        pytest.param(
            {"execution_config_hash": "b" * 64},
            {},
            _EXECUTION_CONFIG_HASH,
            PreflightDenialCode.EXECUTION_CONFIG_MISMATCH,
            id="execution-config",
        ),
        pytest.param(
            {},
            {"maximum_order_notional": 900.0},
            _EXECUTION_CONFIG_HASH,
            PreflightDenialCode.EXECUTION_POLICY_MISMATCH,
            id="policy-content",
        ),
        pytest.param(
            {"broker_environment": BrokerEnvironment.LIVE},
            {},
            _EXECUTION_CONFIG_HASH,
            PreflightDenialCode.BROKER_ENVIRONMENT_MISMATCH,
            id="broker-environment",
        ),
        pytest.param(
            {"account_id": "different-account"},
            {},
            _EXECUTION_CONFIG_HASH,
            PreflightDenialCode.BROKER_ACCOUNT_MISMATCH,
            id="broker-account",
        ),
    ],
)
def test_validate_pre_execution_with_approval_binding_drift_fails_closed(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
    approval_update: dict[str, object],
    policy_update: dict[str, object],
    config_hash: str,
    expected_code: PreflightDenialCode,
) -> None:
    policy = _policy(ExecutionMode.APPROVAL_REQUIRED).model_copy(update=policy_update)
    _enable(execution_repository, now)
    exact_approval = ApprovalRecord(
        decision_id="approval-1",
        plan_id=portfolio_plan.payload.plan_id,
        plan_hash=portfolio_plan.plan_hash,
        decided_at=now,
        decided_by="operator",
        execution_config_hash=_EXECUTION_CONFIG_HASH,
        execution_policy_hash=_policy(ExecutionMode.APPROVAL_REQUIRED).fingerprint(),
        execution_policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        account_id=portfolio_plan.payload.account_id,
        committed_turnover_at_approval=0.0,
    )
    approval = exact_approval.model_copy(update=approval_update)

    result = validate_pre_execution(
        portfolio_plan,
        policy,
        _DecisionStore(approval),
        execution_repository,
        now=now,
        portfolio_unchanged=_unchanged,
        market_unchanged=_unchanged,
        execution_config_hash=config_hash,
    )

    assert expected_code in {denial.code for denial in result.denials}


@pytest.mark.parametrize(
    ("allowed_asset_classes", "expects_constraint_denial"),
    [
        pytest.param((TradableAssetClass.US_ETF,), False, id="etf-allowed"),
        pytest.param((TradableAssetClass.US_EQUITY,), True, id="etf-disallowed"),
    ],
)
def test_validate_pre_execution_uses_each_trade_authoritative_asset_class(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
    allowed_asset_classes: tuple[TradableAssetClass, ...],
    expects_constraint_denial: bool,
) -> None:
    _enable(execution_repository, now)
    etf_trade = portfolio_plan.payload.proposed_trades[0].model_copy(update={"asset_class": TradableAssetClass.US_ETF})
    etf_plan = PortfolioPlan.from_payload(portfolio_plan.payload.model_copy(update={"proposed_trades": (etf_trade,)}))
    policy = _policy(ExecutionMode.APPROVAL_REQUIRED).model_copy(
        update={"allowed_asset_classes": allowed_asset_classes}
    )

    result = validate_pre_execution(
        etf_plan,
        policy,
        _DecisionStore(None),
        execution_repository,
        now=now,
        portfolio_unchanged=_unchanged,
        market_unchanged=_unchanged,
        execution_config_hash=_EXECUTION_CONFIG_HASH,
    )

    codes = {denial.code for denial in result.denials}
    assert (PreflightDenialCode.CONSTRAINT_FAILED in codes) is expects_constraint_denial


def test_validate_pre_execution_applies_cumulative_daily_turnover(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    _enable(execution_repository, now)

    result = validate_pre_execution(
        portfolio_plan,
        _policy(ExecutionMode.APPROVAL_REQUIRED),
        _DecisionStore(None),
        execution_repository,
        now=now,
        portfolio_unchanged=_unchanged,
        market_unchanged=_unchanged,
        execution_config_hash=_EXECUTION_CONFIG_HASH,
        committed_turnover=0.15,
    )

    assert PreflightDenialCode.CONSTRAINT_FAILED in {denial.code for denial in result.denials}
