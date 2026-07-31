"""Tests for immutable portfolio-plan persistence."""

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
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
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
            policy_version="policy-1",
            model_versions={"optimizer": "clarabel-1"},
            target_weights={"SPY": 0.8},
            proposed_trades=(),
            turnover_estimate=0.0,
            evidence_gate_results={"independent_support": True},
            constraint_results={"position_limit": True},
        )
    )


def test_append_persists_hash_validated_plan_idempotently(
    repository: PortfolioPlanRepository,
    plan: PortfolioPlan,
) -> None:
    repository.append(plan)
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
            "UPDATE portfolio_plans SET policy_version = ? WHERE plan_id = ?",
            ("tampered", plan.payload.plan_id),
        )

    with pytest.raises(MalformedPortfolioPlanRecordError):
        repository.get(plan.payload.plan_id)


def test_append_with_simultaneous_conflicting_content_preserves_one_immutable_plan(
    repository: PortfolioPlanRepository,
    plan: PortfolioPlan,
) -> None:
    changed_payload: PortfolioPlanPayload = plan.payload.model_copy(update={"target_weights": {"SPY": 0.7}})
    collision: PortfolioPlan = PortfolioPlan.from_payload(changed_payload)
    barrier = Barrier(2)

    def append(candidate: PortfolioPlan) -> type[Exception] | None:
        barrier.wait()
        try:
            repository.append(candidate)
        except ImmutablePlanCollisionError as error:
            return type(error)
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(append, (plan, collision)))

    assert outcomes.count(None) == 1
    assert outcomes.count(ImmutablePlanCollisionError) == 1
    assert repository.get(plan.payload.plan_id) in (plan, collision)
