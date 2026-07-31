"""Failure-contract tests for durable execution authority state."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import cast

import pytest

from money_pit.execution_control.errors import ExecutionControlError
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.repository import ExecutionClaimBrokerOrderIdError
from money_pit.execution_control.repository import ExecutionClaimStatus
from money_pit.execution_control.repository import ExecutionClaimTimestampError
from money_pit.execution_control.repository import ExecutionClaimTransitionError
from money_pit.execution_control.repository import InvalidExecutionClaimStatusError
from money_pit.execution_control.repository import MissingExecutionControlStateError
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.execution_control.repository import _aware_datetime_column
from money_pit.execution_control.repository import _integer_column
from money_pit.execution_control.repository import _optional_text_column
from money_pit.execution_control.repository import _row_or_none
from money_pit.execution_control.repository import _text_column
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


if TYPE_CHECKING:
    from money_pit.plans.lifecycle import PlanDecision


def _remove_control_state(database: Database) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute("DELETE FROM execution_control WHERE control_id = 1")


def test_get_unknown_plan_returns_none(
    execution_repository: SqliteExecutionAuthorityRepository,
) -> None:
    assert execution_repository.get("missing-plan") is None


def test_latest_for_unknown_plan_returns_none(
    execution_repository: SqliteExecutionAuthorityRepository,
) -> None:
    assert execution_repository.latest_for("missing-plan") is None


def test_append_rejects_unknown_decision_type(
    execution_repository: SqliteExecutionAuthorityRepository,
) -> None:
    with pytest.raises(AssertionError, match="Unhandled plan decision"):
        execution_repository.append(cast("PlanDecision", object()))


def test_get_control_state_without_seed_row_fails_closed(
    database: Database,
    execution_repository: SqliteExecutionAuthorityRepository,
) -> None:
    _remove_control_state(database)

    with pytest.raises(MissingExecutionControlStateError):
        execution_repository.get_control_state()


def test_disable_rejects_enabled_state(
    execution_repository: SqliteExecutionAuthorityRepository,
    now: datetime,
) -> None:
    state = ExecutionControlState(disabled=False, changed_at=now, actor="operator")

    with pytest.raises(ValueError, match="disable requires"):
        execution_repository.disable(state)


def test_disable_without_seed_row_fails_closed(
    database: Database,
    execution_repository: SqliteExecutionAuthorityRepository,
    now: datetime,
) -> None:
    _remove_control_state(database)
    state = ExecutionControlState(disabled=True, changed_at=now, actor="operator")

    with pytest.raises(MissingExecutionControlStateError):
        execution_repository.disable(state)


def test_enable_persists_enabled_state(
    execution_repository: SqliteExecutionAuthorityRepository,
    now: datetime,
) -> None:
    state = ExecutionControlState(disabled=False, changed_at=now, actor="operator")

    execution_repository.enable(state)

    assert execution_repository.get_control_state() == state


def test_enable_rejects_disabled_state(
    execution_repository: SqliteExecutionAuthorityRepository,
    now: datetime,
) -> None:
    state = ExecutionControlState(disabled=True, changed_at=now, actor="operator")

    with pytest.raises(ValueError, match="enable requires"):
        execution_repository.enable(state)


def test_enable_without_seed_row_fails_closed(
    database: Database,
    execution_repository: SqliteExecutionAuthorityRepository,
    now: datetime,
) -> None:
    _remove_control_state(database)
    state = ExecutionControlState(disabled=False, changed_at=now, actor="operator")

    with pytest.raises(MissingExecutionControlStateError):
        execution_repository.enable(state)


def test_update_rejects_unknown_execution_phase(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    execution_repository.claim(persisted_portfolio_plan.plan_hash, "leg-0", claimed_at=now)

    with pytest.raises(InvalidExecutionClaimStatusError):
        execution_repository.update(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            status="unknown",
            updated_at=now,
            broker_order_id=None,
        )


@pytest.mark.parametrize(
    ("current_status", "next_status"),
    [
        (ExecutionClaimStatus.CLAIMED, ExecutionClaimStatus.CLAIMED),
        (ExecutionClaimStatus.CLAIMED, ExecutionClaimStatus.SUBMITTED),
        (ExecutionClaimStatus.CLAIMED, ExecutionClaimStatus.PARTIALLY_FILLED),
        (ExecutionClaimStatus.CLAIMED, ExecutionClaimStatus.FILLED),
        (ExecutionClaimStatus.CLAIMED, ExecutionClaimStatus.FAILED),
        (ExecutionClaimStatus.CLAIMED, ExecutionClaimStatus.CANCELLED),
        (ExecutionClaimStatus.SUBMITTED, ExecutionClaimStatus.SUBMITTED),
        (ExecutionClaimStatus.SUBMITTED, ExecutionClaimStatus.PARTIALLY_FILLED),
        (ExecutionClaimStatus.SUBMITTED, ExecutionClaimStatus.FILLED),
        (ExecutionClaimStatus.SUBMITTED, ExecutionClaimStatus.FAILED),
        (ExecutionClaimStatus.SUBMITTED, ExecutionClaimStatus.CANCELLED),
        (ExecutionClaimStatus.PARTIALLY_FILLED, ExecutionClaimStatus.PARTIALLY_FILLED),
        (ExecutionClaimStatus.PARTIALLY_FILLED, ExecutionClaimStatus.FILLED),
        (ExecutionClaimStatus.PARTIALLY_FILLED, ExecutionClaimStatus.FAILED),
        (ExecutionClaimStatus.PARTIALLY_FILLED, ExecutionClaimStatus.CANCELLED),
    ],
)
def test_update_accepts_explicit_monotonic_transition(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
    current_status: ExecutionClaimStatus,
    next_status: ExecutionClaimStatus,
) -> None:
    execution_repository.claim(persisted_portfolio_plan.plan_hash, "leg-0", claimed_at=now)
    if current_status is not ExecutionClaimStatus.CLAIMED:
        execution_repository.update(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            status=current_status,
            updated_at=now,
            broker_order_id=None,
        )

    execution_repository.update(
        persisted_portfolio_plan.plan_hash,
        "leg-0",
        status=next_status,
        updated_at=now,
        broker_order_id=None,
    )


@pytest.mark.parametrize(
    ("current_status", "next_status"),
    [
        (ExecutionClaimStatus.SUBMITTED, ExecutionClaimStatus.CLAIMED),
        (ExecutionClaimStatus.PARTIALLY_FILLED, ExecutionClaimStatus.CLAIMED),
        (ExecutionClaimStatus.PARTIALLY_FILLED, ExecutionClaimStatus.SUBMITTED),
    ],
)
def test_update_rejects_backward_transition(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
    current_status: ExecutionClaimStatus,
    next_status: ExecutionClaimStatus,
) -> None:
    execution_repository.claim(persisted_portfolio_plan.plan_hash, "leg-0", claimed_at=now)
    execution_repository.update(
        persisted_portfolio_plan.plan_hash,
        "leg-0",
        status=current_status,
        updated_at=now,
        broker_order_id=None,
    )

    with pytest.raises(ExecutionClaimTransitionError):
        execution_repository.update(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            status=next_status,
            updated_at=now,
            broker_order_id=None,
        )


@pytest.mark.parametrize(
    "terminal_status",
    [
        ExecutionClaimStatus.FILLED,
        ExecutionClaimStatus.FAILED,
        ExecutionClaimStatus.CANCELLED,
    ],
)
def test_update_rejects_update_from_terminal_status(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
    terminal_status: ExecutionClaimStatus,
) -> None:
    execution_repository.claim(persisted_portfolio_plan.plan_hash, "leg-0", claimed_at=now)
    execution_repository.update(
        persisted_portfolio_plan.plan_hash,
        "leg-0",
        status=terminal_status,
        updated_at=now,
        broker_order_id=None,
    )

    with pytest.raises(ExecutionClaimTransitionError):
        execution_repository.update(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            status=terminal_status,
            updated_at=now,
            broker_order_id=None,
        )


def test_update_rejects_decreasing_timestamp(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    execution_repository.claim(persisted_portfolio_plan.plan_hash, "leg-0", claimed_at=now)

    with pytest.raises(ExecutionClaimTimestampError):
        execution_repository.update(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            status=ExecutionClaimStatus.SUBMITTED,
            updated_at=now - timedelta(microseconds=1),
            broker_order_id=None,
        )


def test_update_rejects_naive_timestamp(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    execution_repository.claim(persisted_portfolio_plan.plan_hash, "leg-0", claimed_at=now)

    with pytest.raises(ExecutionClaimTimestampError):
        execution_repository.update(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            status=ExecutionClaimStatus.SUBMITTED,
            updated_at=now.replace(tzinfo=None),
            broker_order_id=None,
        )


def test_update_with_naive_durable_timestamp_fails_closed(
    database: Database,
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    execution_repository.claim(persisted_portfolio_plan.plan_hash, "leg-0", claimed_at=now)
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute(
            """UPDATE execution_claims SET updated_at = ?
            WHERE plan_hash = ? AND trade_identity = ?""",
            (now.replace(tzinfo=None).isoformat(), persisted_portfolio_plan.plan_hash, "leg-0"),
        )

    with pytest.raises(ExecutionControlError):
        execution_repository.update(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            status=ExecutionClaimStatus.SUBMITTED,
            updated_at=now,
            broker_order_id=None,
        )


@pytest.mark.parametrize("broker_order_id", [None, "broker-order-2"])
def test_update_rejects_cleared_or_changed_broker_order_id(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
    broker_order_id: str | None,
) -> None:
    execution_repository.claim(persisted_portfolio_plan.plan_hash, "leg-0", claimed_at=now)
    execution_repository.update(
        persisted_portfolio_plan.plan_hash,
        "leg-0",
        status=ExecutionClaimStatus.SUBMITTED,
        updated_at=now,
        broker_order_id="broker-order-1",
    )

    with pytest.raises(ExecutionClaimBrokerOrderIdError):
        execution_repository.update(
            persisted_portfolio_plan.plan_hash,
            "leg-0",
            status=ExecutionClaimStatus.SUBMITTED,
            updated_at=now,
            broker_order_id=broker_order_id,
        )


def test_update_preserves_matching_broker_order_id(
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

    execution_repository.update(
        persisted_portfolio_plan.plan_hash,
        "leg-0",
        status=ExecutionClaimStatus.PARTIALLY_FILLED,
        updated_at=now,
        broker_order_id="broker-order-1",
    )


def test_latest_for_unknown_persisted_decision_fails_closed(
    database: Database,
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """INSERT INTO plan_decisions
            (decision_id, plan_id, plan_hash, decision, decided_at, actor, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "decision-1",
                persisted_portfolio_plan.payload.plan_id,
                persisted_portfolio_plan.plan_hash,
                "unknown",
                now.isoformat(),
                "operator",
                None,
            ),
        )

    with pytest.raises(ExecutionControlError, match="Unknown decision"):
        execution_repository.latest_for(persisted_portfolio_plan.payload.plan_id)


@pytest.fixture
def sqlite_row_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _row_with_value(connection: sqlite3.Connection, value: object) -> sqlite3.Row:
    row = connection.execute("SELECT ? AS value", (value,)).fetchone()
    assert row is not None
    return row


def test__row_or_none_rejects_unexpected_row_representation() -> None:
    with pytest.raises(ExecutionControlError, match="unexpected row representation"):
        _row_or_none(("value",))


def test__text_column_rejects_non_text(sqlite_row_connection: sqlite3.Connection) -> None:
    row = _row_with_value(sqlite_row_connection, 1)
    with pytest.raises(ExecutionControlError, match="must be text"):
        _text_column(row, "value")


def test__optional_text_column_preserves_null(sqlite_row_connection: sqlite3.Connection) -> None:
    row = _row_with_value(sqlite_row_connection, None)
    assert _optional_text_column(row, "value") is None


def test__optional_text_column_rejects_non_text(sqlite_row_connection: sqlite3.Connection) -> None:
    row = _row_with_value(sqlite_row_connection, 1)
    with pytest.raises(ExecutionControlError, match="must be text or null"):
        _optional_text_column(row, "value")


def test__integer_column_rejects_non_integer(sqlite_row_connection: sqlite3.Connection) -> None:
    row = _row_with_value(sqlite_row_connection, "1")
    with pytest.raises(ExecutionControlError, match="must be an integer"):
        _integer_column(row, "value")


def test__aware_datetime_column_rejects_invalid_iso_datetime(
    sqlite_row_connection: sqlite3.Connection,
) -> None:
    row = _row_with_value(sqlite_row_connection, "not-a-datetime")
    with pytest.raises(ExecutionControlError, match="must be an ISO datetime"):
        _aware_datetime_column(row, "value")


def test__aware_datetime_column_rejects_naive_datetime(
    sqlite_row_connection: sqlite3.Connection,
) -> None:
    row = _row_with_value(sqlite_row_connection, "2026-07-29T12:00:00")
    with pytest.raises(ExecutionControlError, match="must be timezone-aware"):
        _aware_datetime_column(row, "value")
