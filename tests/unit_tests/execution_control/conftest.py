"""Fixtures for durable execution-control tests."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


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
        policy_version="policy-1",
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
def persisted_portfolio_plan(
    database: Database,
    portfolio_plan: PortfolioPlan,
) -> PortfolioPlan:
    payload = portfolio_plan.payload
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute(
            """INSERT INTO portfolio_plans (
                plan_id, plan_hash, created_at, expires_at, portfolio_snapshot_id,
                market_snapshot_id, policy_version, model_versions_json,
                prompt_versions_json, plan_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.plan_id,
                portfolio_plan.plan_hash,
                payload.created_at.isoformat(),
                payload.expires_at.isoformat(),
                payload.portfolio_snapshot_id,
                payload.market_snapshot_id,
                payload.policy_version,
                "{}",
                "{}",
                portfolio_plan.model_dump_json(),
            ),
        )
    return portfolio_plan
