"""Tests for SQLite-backed portfolio-plan execution authority."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.repository import ExecutionAlreadyClaimedError
from money_pit.execution_control.repository import ExecutionClaimNotFoundError
from money_pit.execution_control.repository import ExecutionClaimStatus
from money_pit.execution_control.repository import ExecutionClaimTimestampError
from money_pit.execution_control.repository import PersistedPlanIntegrityError
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.plans.lifecycle import approve_plan
from money_pit.plans.lifecycle import reject_plan
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


def test_get_returns_validated_durable_plan(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
) -> None:
    assert execution_repository.get(persisted_portfolio_plan.payload.plan_id) == persisted_portfolio_plan


def test_get_with_tampered_durable_plan_fails_closed(
    database: Database,
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
) -> None:
    serialized: str = persisted_portfolio_plan.model_dump_json().replace('"AAPL":0.2', '"AAPL":0.3')
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute(
            "UPDATE portfolio_plans SET plan_json = ? WHERE plan_id = ?",
            (serialized, persisted_portfolio_plan.payload.plan_id),
        )

    with pytest.raises(PersistedPlanIntegrityError):
        execution_repository.get(persisted_portfolio_plan.payload.plan_id)


def test_latest_for_returns_newest_immutable_decision(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    approval = approve_plan(
        persisted_portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
    )
    rejection = reject_plan(
        persisted_portfolio_plan,
        decision_id="decision-2",
        decided_at=now,
        decided_by="operator",
        reason="risk changed",
    )
    execution_repository.append(approval)
    execution_repository.append(rejection)

    assert execution_repository.latest_for(persisted_portfolio_plan.payload.plan_id) == rejection


def test_get_control_state_starts_disabled(
    execution_repository: SqliteExecutionAuthorityRepository,
) -> None:
    assert execution_repository.get_control_state().disabled


def test_disable_persists_kill_switch_state(
    execution_repository: SqliteExecutionAuthorityRepository,
    now: datetime,
) -> None:
    state = ExecutionControlState(
        disabled=True,
        changed_at=now,
        actor="operator",
        reason="manual halt",
    )

    execution_repository.disable(state)

    assert execution_repository.get_control_state() == state


def test_claim_repeated_trade_fails_closed(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    execution_repository.claim(
        persisted_portfolio_plan.plan_hash,
        "leg-0",
        claimed_at=now,
    )

    with pytest.raises(ExecutionAlreadyClaimedError):
        execution_repository.claim(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            claimed_at=now,
        )


def test_claim_allows_distinct_plan_leg(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    execution_repository.claim(
        persisted_portfolio_plan.plan_hash,
        "leg-0",
        claimed_at=now,
    )

    execution_repository.claim(
        persisted_portfolio_plan.plan_hash,
        "leg-1",
        claimed_at=now,
    )


def test_claim_with_naive_claimed_at_rejects_before_transaction(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    def fail_if_transaction_starts(*args: object, **kwargs: object) -> None:
        raise AssertionError("claim opened a transaction before validating claimed_at")

    monkeypatch.setattr(database, "transaction", fail_if_transaction_starts)

    with pytest.raises(ExecutionClaimTimestampError):
        execution_repository.claim(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            claimed_at=now.replace(tzinfo=None),
        )


def test_claim_normalizes_aware_claimed_at_to_utc(
    database: Database,
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    claimed_at: datetime = now.astimezone(timezone(timedelta(hours=5)))

    execution_repository.claim(
        persisted_portfolio_plan.plan_hash,
        "leg-0",
        claimed_at=claimed_at,
    )

    with database.transaction() as connection:
        row = connection.execute(
            """SELECT claimed_at, updated_at FROM execution_claims
            WHERE plan_hash = ? AND trade_identity = ?""",
            (persisted_portfolio_plan.plan_hash, "leg-0"),
        ).fetchone()
    assert row is not None
    assert tuple(row) == (now.isoformat(), now.isoformat())


def test_update_persists_execution_phase(
    database: Database,
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    execution_repository.claim(persisted_portfolio_plan.plan_hash, "leg-0", claimed_at=now)

    execution_repository.update(
        persisted_portfolio_plan.plan_hash,
        "leg-0",
        status=ExecutionClaimStatus.SUBMITTED,
        updated_at=now,
        broker_order_id="broker-order-1",
    )

    with database.transaction() as connection:
        row = connection.execute(
            """SELECT status, broker_order_id FROM execution_claims
            WHERE plan_hash = ? AND trade_identity = ?""",
            (persisted_portfolio_plan.plan_hash, "leg-0"),
        ).fetchone()
    assert row is not None
    assert tuple(row) == ("submitted", "broker-order-1")


def test_update_without_claim_fails_closed(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    with pytest.raises(ExecutionClaimNotFoundError):
        execution_repository.update(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            status=ExecutionClaimStatus.FAILED,
            updated_at=now,
            broker_order_id=None,
        )
