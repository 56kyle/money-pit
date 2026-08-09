"""Fixtures for durable execution-control tests."""

import sqlite3
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.plans.repository import PortfolioPlanRepository
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.portfolio_plan import PlanTaxEstimate
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade
from money_pit.storage.database import Database


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


@pytest.fixture
def portfolio_plan(now: datetime) -> PortfolioPlan:
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
        turnover_estimate=0.1,
        tax_estimate=PlanTaxEstimate(
            currency="USD",
            estimated_cost=0.0,
            known=True,
        ),
        evidence_gate_results={"independent_support": True},
        constraint_results={"position_limit": True},
    )
    return PortfolioPlan.from_payload(payload)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "money-pit.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def execution_repository(database: Database) -> SqliteExecutionAuthorityRepository:
    return SqliteExecutionAuthorityRepository(database)


@pytest.fixture
def portfolio_plan_repository(database: Database) -> PortfolioPlanRepository:
    return PortfolioPlanRepository(database)


@pytest.fixture
def persisted_portfolio_plan(
    database: Database,
    portfolio_plan_repository: PortfolioPlanRepository,
    portfolio_plan: PortfolioPlan,
) -> PortfolioPlan:
    payload = portfolio_plan.payload
    connection = sqlite3.connect(database.path)
    try:
        _ = connection.execute(
            """INSERT INTO decision_snapshots (
                decision_snapshot_id, decision_hash, run_id, requested_as_of,
                decision_at, known_at,
                portfolio_snapshot_id, market_snapshot_id, risk_snapshot_id,
                liquidity_snapshot_id, tax_snapshot_id, source_config_hash,
                strategy_config_hash, execution_config_hash, policy_version,
                claim_freshness_policy_version,
                verification_result_ids_json, canonical_projection_hashes_json,
                universe_fingerprint, processor_versions_json, calibration_version,
                optimizer_version, trade_generation_version, execution_eligible,
                model_versions_json, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?, '{}', ?, ?, ?, 1, '{}', '{}')""",
            (
                payload.decision_snapshot_id,
                payload.decision_snapshot_hash,
                "run-fixture",
                payload.created_at.isoformat(),
                payload.created_at.isoformat(),
                payload.created_at.isoformat(),
                payload.portfolio_snapshot_id,
                payload.market_snapshot_id,
                "risk-fixture",
                "liquidity-fixture",
                "tax-fixture",
                "source-config-fixture",
                "strategy-config-fixture",
                "execution-config-fixture",
                payload.policy_version,
                "claim-freshness-1",
                "universe-fixture",
                "calibration-1",
                "optimizer-1",
                "trade-generation-1",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    portfolio_plan_repository.append(portfolio_plan)
    return portfolio_plan
