"""Remaining execution-control boundary contracts."""

from dataclasses import dataclass
from datetime import datetime
from typing import cast

import pytest

from money_pit.execution_control.errors import PreflightDeniedError
from money_pit.execution_control.models import ApprovalDecision
from money_pit.execution_control.models import PreflightDenial
from money_pit.execution_control.models import PreflightDenialCode
from money_pit.execution_control.models import PreflightResult
from money_pit.execution_control.models import enabled_execution_state
from money_pit.execution_control.orders import plan_client_order_id
from money_pit.execution_control.preflight import _approval_denials
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.execution_control.service import record_plan_decision
from money_pit.plans.lifecycle import PlanDecision
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan


@dataclass(frozen=True)
class _UnknownDecision:
    plan_hash: str


@dataclass(frozen=True)
class _UnknownDecisionStore:
    plan_hash: str

    def latest_for(self, _plan_id: str) -> PlanDecision | None:
        return cast("PlanDecision", _UnknownDecision(self.plan_hash))


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        maximum_order_notional=1_000.0,
        maximum_daily_turnover=0.2,
    )


def test_plan_client_order_id_rejects_negative_trade_index(portfolio_plan: PortfolioPlan) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        plan_client_order_id(portfolio_plan, -1, "AAPL")


def test_plan_client_order_id_uses_safe_fallback_for_symbol_without_alphanumerics(
    portfolio_plan: PortfolioPlan,
) -> None:
    assert plan_client_order_id(portfolio_plan, 0, "...").endswith("-ASSET")


def test_preflight_result_allowed_is_false_with_denial(
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    PreflightResult(
        plan_id=portfolio_plan.payload.plan_id,
        plan_hash=portfolio_plan.plan_hash,
        checked_at=now,
        denials=(
            PreflightDenial(
                code=PreflightDenialCode.OBSERVE_ONLY,
                detail="observe only",
            ),
        ),
    )


def test_preflight_denied_error_preserves_typed_result(
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    result = PreflightResult(
        plan_id=portfolio_plan.payload.plan_id,
        plan_hash=portfolio_plan.plan_hash,
        checked_at=now,
        denials=(
            PreflightDenial(
                code=PreflightDenialCode.PLAN_EXPIRED,
                detail="expired",
            ),
        ),
    )

    error = PreflightDeniedError(result)

    assert error.result == result

    assert not result.allowed


def test_enabled_execution_state_builds_enabled_default(now: datetime) -> None:
    assert not enabled_execution_state(now).disabled


def test__approval_denials_rejects_unknown_decision_type(portfolio_plan: PortfolioPlan) -> None:
    with pytest.raises(AssertionError, match="Unhandled approval decision"):
        _approval_denials(portfolio_plan, _policy(), _UnknownDecisionStore(portfolio_plan.plan_hash))


def test_record_plan_decision_rejects_unknown_decision_type(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    with pytest.raises(AssertionError, match="Unhandled approval decision"):
        record_plan_decision(
            execution_repository,
            execution_repository,
            plan_id=persisted_portfolio_plan.payload.plan_id,
            decision=cast("ApprovalDecision", "unknown"),
            decided_at=now,
            actor="operator",
            reason=None,
        )
