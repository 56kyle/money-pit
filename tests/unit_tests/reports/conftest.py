"""Fixtures for portfolio-report tests."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.portfolio_plan import PlanTaxEstimate
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade
from money_pit.schemas.portfolio_plan import RejectedCandidate


@pytest.fixture
def portfolio_plan() -> PortfolioPlan:
    now = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)
    payload = PortfolioPlanPayload(
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
        rejected_candidates=(
            RejectedCandidate(
                instrument="XYZ",
                reasons=("evidence unresolved",),
            ),
        ),
        expected_risk_change=-0.02,
        expected_return_change=0.01,
        tax_estimate=PlanTaxEstimate(currency="USD", estimated_cost=12.5, known=True),
        turnover_estimate=0.1,
        evidence_gate_results={"independent_support": True},
        constraint_results={"position_limit": True},
    )
    return PortfolioPlan.from_payload(payload)
