"""Fixtures for portfolio construction tests."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from pytest import FixtureRequest

from money_pit.plans.lifecycle import AuthorizationContext
from money_pit.portfolio.optimizer import OptimizationInput
from money_pit.portfolio.policy import PortfolioPolicy
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade
from money_pit.storage.database import Database


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "money-pit.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


@pytest.fixture
def optimization_input(request: FixtureRequest) -> OptimizationInput:
    return getattr(
        request,
        "param",
        OptimizationInput(
            portfolio_snapshot_id="portfolio-1",
            market_snapshot_id="market-1",
            current_weights={"AAPL": 0.1, "SPY": 0.5, "XOM": 0.1},
            expected_returns={"AAPL": 0.12, "SPY": 0.07, "XOM": 0.08},
            covariance={
                "AAPL": {"AAPL": 0.09, "SPY": 0.018, "XOM": 0.009},
                "SPY": {"AAPL": 0.018, "SPY": 0.04, "XOM": 0.012},
                "XOM": {"AAPL": 0.009, "SPY": 0.012, "XOM": 0.06},
            },
            sectors={"AAPL": "technology", "SPY": "broad_market", "XOM": "energy"},
            satellite_instruments=frozenset({"AAPL", "XOM"}),
            tax_cost_rates={"AAPL": 0.0, "SPY": 0.0, "XOM": 0.0},
        ),
    )


@pytest.fixture
def portfolio_policy(request: FixtureRequest) -> PortfolioPolicy:
    return getattr(
        request,
        "param",
        PortfolioPolicy(
            policy_version="policy-1",
            risk_aversion=0.5,
            turnover_penalty=0.01,
            tax_penalty=1.0,
            minimum_cash_weight=0.1,
            maximum_position_weight=0.6,
            maximum_satellite_weight=0.4,
            maximum_turnover=0.3,
            minimum_trade_weight=0.0,
            maximum_position_change=0.25,
            minimum_core_weights={"SPY": 0.4},
            maximum_sector_weights={
                "broad_market": 0.6,
                "energy": 0.25,
                "technology": 0.3,
            },
            accept_optimal_inaccurate=False,
            feasibility_tolerance=1e-6,
        ),
    )


@pytest.fixture
def portfolio_plan_payload(
    request: FixtureRequest,
    now: datetime,
) -> PortfolioPlanPayload:
    return getattr(
        request,
        "param",
        PortfolioPlanPayload(
            plan_id="plan-1",
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            portfolio_snapshot_id="portfolio-1",
            market_snapshot_id="market-1",
            policy_version="policy-1",
            model_versions={"analysis": "model-1"},
            prompt_versions={"analysis": "prompt-1"},
            target_weights={"AAPL": 0.2, "SPY": 0.6},
            proposed_trades=(
                ProposedTrade(
                    instrument="AAPL",
                    side="buy",
                    quantity=2.0,
                    estimated_notional=400.0,
                    tax_cost_known=True,
                ),
            ),
            turnover_estimate=0.1,
            tax_estimates={"known": True},
            evidence_gate_results={"independent_support": True},
            constraint_results={"position_limit": True},
        ),
    )


@pytest.fixture
def portfolio_plan(portfolio_plan_payload: PortfolioPlanPayload) -> PortfolioPlan:
    return PortfolioPlan.from_payload(portfolio_plan_payload)


@pytest.fixture
def authorization_context(
    portfolio_plan_payload: PortfolioPlanPayload,
    now: datetime,
) -> AuthorizationContext:
    return AuthorizationContext(
        evaluated_at=now + timedelta(minutes=1),
        portfolio_snapshot_id=portfolio_plan_payload.portfolio_snapshot_id,
        market_snapshot_id=portfolio_plan_payload.market_snapshot_id,
        policy_version=portfolio_plan_payload.policy_version,
        evidence_gate_results=portfolio_plan_payload.evidence_gate_results,
        constraint_results=portfolio_plan_payload.constraint_results,
        execution_enabled=True,
        asset_classes={"AAPL": "us_equity"},
        committed_turnover=0.0,
    )


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
