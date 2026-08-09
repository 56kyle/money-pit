"""Fixtures for portfolio-plan lifecycle tests."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.portfolio_plan import PlanTaxEstimate
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


@pytest.fixture
def portfolio_plan_payload(now: datetime) -> PortfolioPlanPayload:
    return PortfolioPlanPayload(
        plan_id="plan-1",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        portfolio_snapshot_id="portfolio-1",
        market_snapshot_id="market-1",
        decision_snapshot_id="decision-1",
        decision_snapshot_hash="1" * 64,
        account_id="paper-account",
        broker_environment=BrokerEnvironment.PAPER,
        policy_version="policy-1",
        target_weights={"AAPL": 0.2, "SPY": 0.6},
        proposed_trades=(
            ProposedTrade(
                instrument="AAPL",
                asset_class=TradableAssetClass.US_EQUITY,
                side="buy",
                quantity=2.0,
                estimated_notional=400.0,
                tax_cost_known=True,
            ),
        ),
        turnover_estimate=0.1,
        tax_estimate=PlanTaxEstimate(currency="USD", estimated_cost=0.0, known=True),
        evidence_gate_results={"independent_support": True},
        constraint_results={"position_limit": True},
    )


@pytest.fixture
def portfolio_plan(portfolio_plan_payload: PortfolioPlanPayload) -> PortfolioPlan:
    return PortfolioPlan.from_payload(portfolio_plan_payload)


@pytest.fixture
def approval_required_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        maximum_order_notional=1_000.0,
        maximum_daily_turnover=0.2,
    )


@pytest.fixture
def autonomous_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=ExecutionMode.AUTONOMOUS,
        maximum_order_notional=1_000.0,
        maximum_daily_turnover=0.2,
    )
