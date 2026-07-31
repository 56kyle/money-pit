"""Module containing SQLite-backed portfolio-plan execution authority."""

import sqlite3
from datetime import UTC
from datetime import datetime
from enum import StrEnum
from typing import cast

from pydantic import ValidationError

from money_pit.execution_control.errors import ExecutionControlError
from money_pit.execution_control.models import ExecutionControlState
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import PlanDecision
from money_pit.plans.lifecycle import RejectionRecord
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


class MissingExecutionControlStateError(ExecutionControlError):
    """Raised when the seeded singleton kill-switch row is absent."""


class PersistedPlanIntegrityError(ExecutionControlError):
    """Raised when a stored portfolio plan is malformed or tampered with."""


class ExecutionAlreadyClaimedError(ExecutionControlError):
    """Raised when a plan trade already has a durable execution claim."""


class ExecutionClaimNotFoundError(ExecutionControlError):
    """Raised when an execution-claim update has no durable claim."""


class InvalidExecutionClaimStatusError(ExecutionControlError):
    """Raised when a requested or persisted execution phase is unknown."""


class ExecutionClaimTransitionError(ExecutionControlError):
    """Raised when an execution claim cannot enter the requested phase."""


class ExecutionClaimTimestampError(ExecutionControlError):
    """Raised when an execution-claim update timestamp is invalid."""


class ExecutionClaimBrokerOrderIdError(ExecutionControlError):
    """Raised when an execution-claim update changes its broker order ID."""


class ExecutionClaimStatus(StrEnum):
    """Durable phases accepted by the execution_claims table."""

    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ALLOWED_EXECUTION_CLAIM_TRANSITIONS: dict[ExecutionClaimStatus, frozenset[ExecutionClaimStatus]] = {
    ExecutionClaimStatus.CLAIMED: frozenset(ExecutionClaimStatus),
    ExecutionClaimStatus.SUBMITTED: frozenset(
        {
            ExecutionClaimStatus.SUBMITTED,
            ExecutionClaimStatus.PARTIALLY_FILLED,
            ExecutionClaimStatus.FILLED,
            ExecutionClaimStatus.FAILED,
            ExecutionClaimStatus.CANCELLED,
        }
    ),
    ExecutionClaimStatus.PARTIALLY_FILLED: frozenset(
        {
            ExecutionClaimStatus.PARTIALLY_FILLED,
            ExecutionClaimStatus.FILLED,
            ExecutionClaimStatus.FAILED,
            ExecutionClaimStatus.CANCELLED,
        }
    ),
}


class SqliteExecutionAuthorityRepository:
    """Implement plan, decision, kill-switch, and idempotency protocols in SQLite."""

    def __init__(self, database: Database) -> None:
        """Initialize the repository over one configured database boundary."""
        self._database: Database = database

    def get(self, plan_id: str) -> PortfolioPlan | None:
        """Return a validated durable plan, failing closed on corrupt content."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = _row_or_none(
                cast(
                    "object",
                    connection.execute(
                        "SELECT plan_json FROM portfolio_plans WHERE plan_id = ?", (plan_id,)
                    ).fetchone(),
                )
            )
        if row is None:
            return None
        try:
            return PortfolioPlan.model_validate_json(_text_column(row, "plan_json"))
        except ValidationError as error:
            raise PersistedPlanIntegrityError(f"Persisted plan {plan_id!r} is invalid.") from error

    def append(self, record: PlanDecision) -> None:
        """Persist one immutable approval or rejection record."""
        record_value: object = record
        if isinstance(record_value, ApprovalRecord):
            decision, actor, reason = "approved", record_value.decided_by, None
        elif isinstance(record_value, RejectionRecord):
            decision, actor, reason = "rejected", record_value.decided_by, record_value.reason
        else:
            raise AssertionError(f"Unhandled plan decision: {record_value!r}")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """INSERT INTO plan_decisions
                (decision_id, plan_id, plan_hash, decision, decided_at, actor, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.decision_id,
                    record.plan_id,
                    record.plan_hash,
                    decision,
                    record.decided_at.isoformat(),
                    actor,
                    reason,
                ),
            )

    def latest_for(self, plan_id: str) -> PlanDecision | None:
        """Return the newest immutable decision for a plan."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = _row_or_none(
                cast(
                    "object",
                    connection.execute(
                        """SELECT decision_id, plan_id, plan_hash, decision, decided_at, actor, reason
                    FROM plan_decisions WHERE plan_id = ?
                    ORDER BY decided_at DESC, decision_id DESC LIMIT 1""",
                        (plan_id,),
                    ).fetchone(),
                )
            )
        if row is None:
            return None
        fields: dict[str, object] = {
            "decision_id": _text_column(row, "decision_id"),
            "plan_id": _text_column(row, "plan_id"),
            "plan_hash": _text_column(row, "plan_hash"),
            "decided_at": _aware_datetime_column(row, "decided_at"),
            "decided_by": _text_column(row, "actor"),
        }
        if _text_column(row, "decision") == "approved":
            return ApprovalRecord.model_validate(fields)
        if _text_column(row, "decision") == "rejected":
            fields["reason"] = _text_column(row, "reason")
            return RejectionRecord.model_validate(fields)
        raise ExecutionControlError(f"Unknown decision {_column(row, 'decision')!r}.")

    def get_control_state(self) -> ExecutionControlState:
        """Return the seeded kill-switch row, failing closed when absent."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = _row_or_none(
                cast(
                    "object",
                    connection.execute(
                        """SELECT execution_disabled, changed_at, changed_by, reason
                    FROM execution_control WHERE control_id = 1"""
                    ).fetchone(),
                )
            )
        if row is None:
            raise MissingExecutionControlStateError("Execution-control state is absent.")
        return ExecutionControlState(
            disabled=bool(_integer_column(row, "execution_disabled")),
            changed_at=_aware_datetime_column(row, "changed_at"),
            actor=_text_column(row, "changed_by"),
            reason=_optional_text_column(row, "reason"),
        )

    def disable(self, state: ExecutionControlState) -> None:
        """Persist a disabled kill-switch state without providing an enable path."""
        if not state.disabled:
            raise ValueError("disable requires a disabled ExecutionControlState.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor: sqlite3.Cursor = connection.execute(
                """UPDATE execution_control SET execution_disabled = 1,
                changed_at = ?, changed_by = ?, reason = ? WHERE control_id = 1""",
                (state.changed_at.isoformat(), state.actor, state.reason),
            )
            if cursor.rowcount != 1:
                raise MissingExecutionControlStateError("Execution-control state is absent.")

    def enable(self, state: ExecutionControlState) -> None:
        """Enable the switch without granting authority to any plan."""
        if state.disabled:
            raise ValueError("enable requires an enabled ExecutionControlState.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor: sqlite3.Cursor = connection.execute(
                """UPDATE execution_control SET execution_disabled = 0,
                changed_at = ?, changed_by = ?, reason = ? WHERE control_id = 1""",
                (state.changed_at.isoformat(), state.actor, state.reason),
            )
            if cursor.rowcount != 1:
                raise MissingExecutionControlStateError("Execution-control state is absent.")

    def claim(self, plan_hash: str, trade_identity: str, *, claimed_at: datetime) -> None:
        """Atomically claim one exact plan trade before broker submission."""
        if claimed_at.tzinfo is None or claimed_at.utcoffset() is None:
            raise ExecutionClaimTimestampError("Execution-claim claimed_at must be timezone-aware.")
        timestamp: str = claimed_at.astimezone(UTC).isoformat()
        with self._database.transaction(TransactionMode.WRITE) as connection:
            existing: sqlite3.Row | None = _row_or_none(
                cast(
                    "object",
                    connection.execute(
                        "SELECT status FROM execution_claims WHERE plan_hash = ? AND trade_identity = ?",
                        (plan_hash, trade_identity),
                    ).fetchone(),
                )
            )
            if existing is not None:
                raise ExecutionAlreadyClaimedError(
                    f"Trade {trade_identity!r} for plan {plan_hash!r} is already claimed."
                )
            _ = connection.execute(
                """INSERT INTO execution_claims
                (execution_claim_id, plan_hash, trade_identity, status, claimed_at, updated_at)
                VALUES (?, ?, ?, 'claimed', ?, ?)""",
                (f"{plan_hash}:{trade_identity}", plan_hash, trade_identity, timestamp, timestamp),
            )

    def update(
        self,
        plan_hash: str,
        trade_identity: str,
        *,
        status: str,
        updated_at: datetime,
        broker_order_id: str | None,
    ) -> None:
        """Persist a valid monotonic phase update for one claimed trade."""
        try:
            resolved_status: ExecutionClaimStatus = ExecutionClaimStatus(status)
        except ValueError as error:
            raise InvalidExecutionClaimStatusError(f"Unknown execution-claim status {status!r}.") from error
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ExecutionClaimTimestampError("Execution-claim updated_at must be timezone-aware.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            row: sqlite3.Row | None = _row_or_none(
                cast(
                    "object",
                    connection.execute(
                        """SELECT status, updated_at, broker_order_id FROM execution_claims
                        WHERE plan_hash = ? AND trade_identity = ?""",
                        (plan_hash, trade_identity),
                    ).fetchone(),
                )
            )
            if row is None:
                raise ExecutionClaimNotFoundError(f"No execution claim exists for {plan_hash!r}/{trade_identity!r}.")
            stored_status_value: str = _text_column(row, "status")
            try:
                stored_status: ExecutionClaimStatus = ExecutionClaimStatus(stored_status_value)
            except ValueError as error:
                raise InvalidExecutionClaimStatusError(
                    f"Stored execution-claim status {stored_status_value!r} is unknown."
                ) from error
            allowed_statuses: frozenset[ExecutionClaimStatus] | None = _ALLOWED_EXECUTION_CLAIM_TRANSITIONS.get(
                stored_status
            )
            if allowed_statuses is None or resolved_status not in allowed_statuses:
                message: str = (
                    f"Execution claim cannot transition from {stored_status.value!r} to "
                    + f"{resolved_status.value!r}."
                )
                raise ExecutionClaimTransitionError(message)
            stored_updated_at: datetime = _aware_datetime_column(row, "updated_at")
            if updated_at < stored_updated_at:
                raise ExecutionClaimTimestampError("Execution-claim updated_at cannot precede its durable timestamp.")
            stored_broker_order_id: str | None = _optional_text_column(row, "broker_order_id")
            if stored_broker_order_id is not None and broker_order_id != stored_broker_order_id:
                raise ExecutionClaimBrokerOrderIdError(
                    "An execution claim's broker order ID cannot change or be cleared."
                )
            cursor: sqlite3.Cursor = connection.execute(
                """UPDATE execution_claims SET status = ?, updated_at = ?, broker_order_id = ?
                WHERE plan_hash = ? AND trade_identity = ?""",
                (resolved_status.value, updated_at.isoformat(), broker_order_id, plan_hash, trade_identity),
            )
            if cursor.rowcount != 1:
                raise ExecutionClaimNotFoundError(f"No execution claim exists for {plan_hash!r}/{trade_identity!r}.")


def _row_or_none(value: object) -> sqlite3.Row | None:
    row: object = value
    if row is None or isinstance(row, sqlite3.Row):
        return row
    raise ExecutionControlError("SQLite returned an unexpected row representation.")


def _column(row: sqlite3.Row, name: str) -> object:
    return cast("object", row[name])


def _text_column(row: sqlite3.Row, name: str) -> str:
    value: object = _column(row, name)
    if not isinstance(value, str):
        raise ExecutionControlError(f"Stored {name} must be text.")
    return value


def _optional_text_column(row: sqlite3.Row, name: str) -> str | None:
    value: object = _column(row, name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionControlError(f"Stored {name} must be text or null.")
    return value


def _integer_column(row: sqlite3.Row, name: str) -> int:
    value: object = _column(row, name)
    if not isinstance(value, int):
        raise ExecutionControlError(f"Stored {name} must be an integer.")
    return value


def _aware_datetime_column(row: sqlite3.Row, name: str) -> datetime:
    value: str = _text_column(row, name)
    try:
        parsed: datetime = datetime.fromisoformat(value)
    except ValueError as error:
        raise ExecutionControlError(f"Stored {name} must be an ISO datetime.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionControlError(f"Stored {name} must be timezone-aware.")
    return parsed
