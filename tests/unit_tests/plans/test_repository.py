"""Tests for immutable portfolio-plan persistence."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest

from money_pit.plans.repository import ImmutablePlanCollisionError
from money_pit.plans.repository import MalformedPortfolioPlanRecordError
from money_pit.plans.repository import PortfolioPlanIntegrityError
from money_pit.plans.repository import PortfolioPlanRepository
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.portfolio_plan import PlanTaxEstimate
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
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
                "decision-1",
                "1" * 64,
                "run-fixture",
                "2026-07-29T00:00:00+00:00",
                "2026-07-29T00:00:00+00:00",
                "2026-07-29T00:00:00+00:00",
                "portfolio-1",
                "market-1",
                "risk-fixture",
                "liquidity-fixture",
                "tax-fixture",
                "source-config-fixture",
                "strategy-config-fixture",
                "execution-config-fixture",
                "policy-1",
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
    return database


@pytest.fixture
def repository(database: Database) -> PortfolioPlanRepository:
    return PortfolioPlanRepository(database)


@pytest.fixture
def plan() -> PortfolioPlan:
    created_at = datetime(2026, 7, 29, tzinfo=UTC)
    return PortfolioPlan.from_payload(
        PortfolioPlanPayload(
            plan_id="plan-1",
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=30),
            portfolio_snapshot_id="portfolio-1",
            market_snapshot_id="market-1",
            decision_snapshot_id="decision-1",
            decision_snapshot_hash="1" * 64,
            account_id="paper-account",
            broker_environment=BrokerEnvironment.PAPER,
            policy_version="policy-1",
            model_versions={"optimizer": "clarabel-1"},
            target_weights={"SPY": 0.8},
            proposed_trades=(),
            turnover_estimate=0.0,
            tax_estimate=PlanTaxEstimate(currency="USD", estimated_cost=0.0, known=True),
            evidence_gate_results={"independent_support": True},
            constraint_results={"position_limit": True},
        )
    )


def test_append_persists_hash_validated_plan_idempotently(
    repository: PortfolioPlanRepository,
    plan: PortfolioPlan,
) -> None:
    _ = repository.append(plan)
    repository.append(plan)

    assert repository.get(plan.payload.plan_id) == plan


def test_append_with_tampered_in_memory_hash_fails_closed(
    repository: PortfolioPlanRepository,
    plan: PortfolioPlan,
) -> None:
    tampered: PortfolioPlan = plan.model_copy(update={"plan_hash": "0" * 64})

    with pytest.raises(PortfolioPlanIntegrityError):
        repository.append(tampered)

    assert repository.get(plan.payload.plan_id) is None


def test_append_with_reused_plan_id_and_different_content_fails_closed(
    repository: PortfolioPlanRepository,
    plan: PortfolioPlan,
) -> None:
    repository.append(plan)
    changed_payload: PortfolioPlanPayload = plan.payload.model_copy(update={"target_weights": {"SPY": 0.7}})

    with pytest.raises(ImmutablePlanCollisionError):
        repository.append(PortfolioPlan.from_payload(changed_payload))


def test_get_with_tampered_indexed_metadata_fails_closed(
    database: Database,
    repository: PortfolioPlanRepository,
    plan: PortfolioPlan,
) -> None:
    repository.append(plan)
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            "UPDATE portfolio_plans SET created_at = ? WHERE plan_id = ?",
            ("2026-07-28T00:00:00+00:00", plan.payload.plan_id),
        )

    with pytest.raises(MalformedPortfolioPlanRecordError):
        _ = repository.get(plan.payload.plan_id)


def test_append_with_simultaneous_conflicting_content_preserves_one_immutable_plan(
    repository: PortfolioPlanRepository,
    plan: PortfolioPlan,
) -> None:
    changed_payload: PortfolioPlanPayload = plan.payload.model_copy(update={"target_weights": {"SPY": 0.7}})
    collision: PortfolioPlan = PortfolioPlan.from_payload(changed_payload)
    barrier = Barrier(2)

    def append(candidate: PortfolioPlan) -> type[Exception] | None:
        _ = barrier.wait()
        try:
            _ = repository.append(candidate)
        except ImmutablePlanCollisionError as error:
            return type(error)
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(append, (plan, collision)))

    assert outcomes.count(None) == 1
    assert outcomes.count(ImmutablePlanCollisionError) == 1
    assert repository.get(plan.payload.plan_id) in (plan, collision)
