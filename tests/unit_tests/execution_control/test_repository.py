"""Tests for SQLite-backed portfolio-plan execution authority."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from threading import Barrier
from typing import TYPE_CHECKING
from typing import cast


if TYPE_CHECKING:
    import sqlite3

import pytest

from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.models import ExecutionEvent
from money_pit.execution_control.models import ExecutionEventPhase
from money_pit.execution_control.repository import ExecutionAlreadyClaimedError
from money_pit.execution_control.repository import ExecutionClaimNotFoundError
from money_pit.execution_control.repository import ExecutionClaimStatus
from money_pit.execution_control.repository import ExecutionClaimTimestampError
from money_pit.execution_control.repository import PersistedPlanIntegrityError
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.execution_control.repository import TurnoverCapExceededError
from money_pit.execution_control.repository import TurnoverReservationRepository
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import reject_plan
from money_pit.plans.repository import PortfolioPlanRepository
from money_pit.schemas.execution_policy import BrokerEnvironment
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
        _ = connection.execute(
            "UPDATE portfolio_plans SET payload_json = ? WHERE plan_id = ?",
            (serialized, persisted_portfolio_plan.payload.plan_id),
        )

    with pytest.raises(PersistedPlanIntegrityError):
        _ = execution_repository.get(persisted_portfolio_plan.payload.plan_id)


def test_latest_for_returns_newest_immutable_decision(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    approval = ApprovalRecord(
        decision_id="decision-1",
        plan_id=persisted_portfolio_plan.payload.plan_id,
        plan_hash=persisted_portfolio_plan.plan_hash,
        decided_at=now,
        decided_by="operator",
        execution_config_hash="a" * 64,
        execution_policy_hash="b" * 64,
        execution_policy_version="execution-policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        account_id="paper-account",
        committed_turnover_at_approval=0.25,
    )
    rejection = reject_plan(
        persisted_portfolio_plan,
        decision_id="decision-2",
        decided_at=now,
        decided_by="operator",
        reason="risk changed",
    )
    execution_repository.append(approval)
    assert execution_repository.latest_for(persisted_portfolio_plan.payload.plan_id) == approval
    execution_repository.append(rejection)

    assert execution_repository.latest_for(persisted_portfolio_plan.payload.plan_id) == rejection
    later_approval = approval.model_copy(update={"decision_id": "decision-3", "decided_at": now + timedelta(minutes=1)})
    execution_repository.append(later_approval)
    assert execution_repository.latest_for(persisted_portfolio_plan.payload.plan_id) == later_approval
    assert (
        execution_repository.rejection_for(
            persisted_portfolio_plan.payload.plan_id,
            persisted_portfolio_plan.plan_hash,
        )
        == rejection
    )


def test_turnover_reservation_releases_capacity_before_submission(
    database: Database,
    portfolio_plan_repository: PortfolioPlanRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    second_plan = PortfolioPlan.from_payload(persisted_portfolio_plan.payload.model_copy(update={"plan_id": "plan-2"}))
    portfolio_plan_repository.append(second_plan)
    reservations = TurnoverReservationRepository(database)
    first = reservations.reserve_turnover(
        account_id="paper-account",
        trading_date=now.date(),
        plan_id=persisted_portfolio_plan.payload.plan_id,
        plan_hash=persisted_portfolio_plan.plan_hash,
        amount_fraction=0.1,
        maximum_fraction=0.15,
        reserved_at=now,
    )

    with pytest.raises(TurnoverCapExceededError):
        _ = reservations.reserve_turnover(
            account_id="paper-account",
            trading_date=now.date(),
            plan_id=second_plan.payload.plan_id,
            plan_hash=second_plan.plan_hash,
            amount_fraction=0.1,
            maximum_fraction=0.15,
            reserved_at=now,
        )

    released = reservations.release(first.reservation_id, released_at=now)
    second = reservations.reserve_turnover(
        account_id="paper-account",
        trading_date=now.date(),
        plan_id=second_plan.payload.plan_id,
        plan_hash=second_plan.plan_hash,
        amount_fraction=0.1,
        maximum_fraction=0.15,
        reserved_at=now,
    )

    assert released.status.value == "released"
    assert second.status.value == "reserved"


def test_turnover_reservation_atomically_enforces_one_account_day_cap_under_concurrency(
    database: Database,
    portfolio_plan_repository: PortfolioPlanRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    second_plan = PortfolioPlan.from_payload(
        persisted_portfolio_plan.payload.model_copy(update={"plan_id": "plan-concurrent-2"})
    )
    _ = portfolio_plan_repository.append(second_plan)
    barrier = Barrier(2)

    def reserve(plan: PortfolioPlan) -> str:
        _ = barrier.wait()
        try:
            _ = TurnoverReservationRepository(database).reserve_turnover(
                account_id="paper-account",
                trading_date=now.date(),
                plan_id=plan.payload.plan_id,
                plan_hash=plan.plan_hash,
                amount_fraction=0.1,
                maximum_fraction=0.15,
                reserved_at=now,
            )
        except TurnoverCapExceededError:
            return "denied"
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, (persisted_portfolio_plan, second_plan)))

    assert sorted(outcomes) == ["denied", "reserved"]


def test_committed_turnover_uses_eastern_calendar_across_dst_utc_midnight(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan_repository: PortfolioPlanRepository,
    persisted_portfolio_plan: PortfolioPlan,
) -> None:
    submitted_at = datetime(2026, 3, 9, 3, 30, tzinfo=timezone.utc)
    other_plan = PortfolioPlan.from_payload(
        persisted_portfolio_plan.payload.model_copy(update={"plan_id": "plan-eastern-boundary"})
    )
    portfolio_plan_repository.append(other_plan)
    execution_repository.append_event(
        ExecutionEvent(
            event_id="submitted-eastern-boundary",
            plan_id=other_plan.payload.plan_id,
            plan_hash=other_plan.plan_hash,
            phase=ExecutionEventPhase.SUBMITTED,
            occurred_at=submitted_at,
        )
    )

    same_eastern_day = execution_repository.committed_turnover_excluding_plan(
        persisted_portfolio_plan.payload.plan_id,
        submitted_at,
    )
    next_eastern_day = execution_repository.committed_turnover_excluding_plan(
        persisted_portfolio_plan.payload.plan_id,
        submitted_at + timedelta(hours=1),
    )

    assert (same_eastern_day, next_eastern_day) == (other_plan.payload.turnover_estimate, 0)


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
        policy_version="policy-1",
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
        del args, kwargs
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
        row = cast(
            "sqlite3.Row | None",
            connection.execute(
                """SELECT claimed_at, updated_at FROM execution_claims
            WHERE plan_hash = ? AND trade_identity = ?""",
                (persisted_portfolio_plan.plan_hash, "leg-0"),
            ).fetchone(),
        )
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
        row = cast(
            "sqlite3.Row | None",
            connection.execute(
                """SELECT status, broker_order_id FROM execution_claims
            WHERE plan_hash = ? AND trade_identity = ?""",
                (persisted_portfolio_plan.plan_hash, "leg-0"),
            ).fetchone(),
        )
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
