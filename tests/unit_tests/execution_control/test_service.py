"""Tests for execution-control application services."""

from datetime import datetime

import pytest

from money_pit.execution_control.errors import PlanNotFoundError
from money_pit.execution_control.models import ApprovalDecision
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.execution_control.service import ApprovalBinding
from money_pit.execution_control.service import disable_execution
from money_pit.execution_control.service import record_plan_decision
from money_pit.execution_control.service import require_plan
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import RejectionRecord
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan


def _approval_binding(plan: PortfolioPlan, now: datetime) -> ApprovalBinding:
    policy = ExecutionPolicy(
        policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        maximum_order_notional=1_000,
        maximum_daily_turnover=0.2,
    )
    portfolio = PortfolioStateSnapshot.from_payload(
        PortfolioStatePayload(
            account_id=plan.payload.account_id,
            broker_environment=BrokerEnvironment.PAPER,
            captured_at=now,
            available_cash=1_000,
            positions=(),
            open_order_ids=(),
        )
    )
    return ApprovalBinding(
        execution_config_hash="e" * 64,
        execution_policy=policy,
        portfolio=portfolio,
        committed_turnover=0.0,
    )


def test_require_plan_returns_durable_plan(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
) -> None:
    assert require_plan(execution_repository, persisted_portfolio_plan.payload.plan_id) == persisted_portfolio_plan


def test_require_plan_with_missing_identifier_raises(
    execution_repository: SqliteExecutionAuthorityRepository,
) -> None:
    with pytest.raises(PlanNotFoundError):
        _ = require_plan(execution_repository, "missing")


def test_record_plan_decision_persists_approval(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    record = record_plan_decision(
        execution_repository,
        execution_repository,
        plan_id=persisted_portfolio_plan.payload.plan_id,
        decision=ApprovalDecision.APPROVED,
        decided_at=now,
        actor="operator",
        reason=None,
        approval_binding=_approval_binding(persisted_portfolio_plan, now),
    )
    assert isinstance(record, ApprovalRecord)


def test_record_plan_decision_persists_rejection_reason(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    record = record_plan_decision(
        execution_repository,
        execution_repository,
        plan_id=persisted_portfolio_plan.payload.plan_id,
        decision=ApprovalDecision.REJECTED,
        decided_at=now,
        actor="operator",
        reason="risk changed",
    )
    assert isinstance(record, RejectionRecord)
    assert record.reason == "risk changed"


def test_record_plan_decision_rejection_without_reason_rejects_value(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    with pytest.raises(ValueError, match="non-blank reason"):
        _ = record_plan_decision(
            execution_repository,
            execution_repository,
            plan_id=persisted_portfolio_plan.payload.plan_id,
            decision=ApprovalDecision.REJECTED,
            decided_at=now,
            actor="operator",
            reason=" ",
        )


def test_disable_execution_persists_state(
    execution_repository: SqliteExecutionAuthorityRepository,
    now: datetime,
) -> None:
    state = disable_execution(
        execution_repository,
        changed_at=now,
        actor="operator",
        reason="manual halt",
    )
    assert state == execution_repository.get_control_state()
