"""Module containing SQLite-backed portfolio-plan execution authority."""

import hashlib
import json
import sqlite3
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time as datetime_time
from datetime import timedelta
from enum import StrEnum
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter
from pydantic import ValidationError

from money_pit.execution_control.errors import ExecutionControlError
from money_pit.execution_control.errors import ExecutionJournalError
from money_pit.execution_control.models import ExecutionClaim
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.models import ExecutionEvent
from money_pit.execution_control.models import ExecutionEventPhase
from money_pit.execution_control.models import TurnoverReservation
from money_pit.execution_control.models import TurnoverReservationStatus
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


class TurnoverCapExceededError(ExecutionControlError):
    """Raised when a reservation would exceed the account's trading-day cap."""


class TurnoverReservationConflictError(ExecutionControlError):
    """Raised when a reservation identity or lifecycle transition conflicts."""


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
                        "SELECT payload_json FROM portfolio_plans WHERE plan_id = ?", (plan_id,)
                    ).fetchone(),
                )
            )
        if row is None:
            return None
        try:
            return PortfolioPlan.model_validate_json(_text_column(row, "payload_json"))
        except ValidationError as error:
            raise PersistedPlanIntegrityError(f"Persisted plan {plan_id!r} is invalid.") from error

    def append(self, record: PlanDecision) -> None:
        """Persist one immutable approval or rejection record."""
        if isinstance(record, ApprovalRecord):
            decision, actor, reason = "approved", record.decided_by, None
            approval_bindings: tuple[object, ...] = (
                record.execution_config_hash,
                record.execution_policy_hash,
                record.execution_policy_version,
                record.broker_environment.value,
                record.account_id,
                record.committed_turnover_at_approval,
            )
        else:
            decision, actor, reason = "rejected", record.decided_by, record.reason
            approval_bindings = (None, None, None, None, None, None)
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """INSERT INTO plan_decisions
                (decision_id, plan_id, plan_hash, decision, decided_at, actor, reason, expires_at,
                 execution_config_hash, execution_policy_hash, execution_policy_version,
                 broker_environment, account_id, committed_turnover_at_approval)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.decision_id,
                    record.plan_id,
                    record.plan_hash,
                    decision,
                    record.decided_at.isoformat(),
                    actor,
                    "" if reason is None else reason,
                    None,
                    *approval_bindings,
                ),
            )

    def latest_for(self, plan_id: str) -> PlanDecision | None:
        """Return the newest immutable decision for a plan."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = _row_or_none(
                cast(
                    "object",
                    connection.execute(
                        """SELECT decision_id, plan_id, plan_hash, decision, decided_at, actor, reason,
                               execution_config_hash, execution_policy_hash,
                               execution_policy_version, broker_environment, account_id,
                               committed_turnover_at_approval
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
            fields.update(
                execution_config_hash=_text_column(row, "execution_config_hash"),
                execution_policy_hash=_text_column(row, "execution_policy_hash"),
                execution_policy_version=_text_column(row, "execution_policy_version"),
                broker_environment=_text_column(row, "broker_environment"),
                account_id=_text_column(row, "account_id"),
                committed_turnover_at_approval=_float_column(row, "committed_turnover_at_approval"),
            )
            return ApprovalRecord.model_validate(fields)
        if _text_column(row, "decision") == "rejected":
            fields["reason"] = _text_column(row, "reason")
            return RejectionRecord.model_validate(fields)
        raise ExecutionControlError(f"Unknown decision {_column(row, 'decision')!r}.")

    def rejection_for(self, plan_id: str, plan_hash: str) -> RejectionRecord | None:
        """Return an unconditional operator veto for one exact plan when present."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = _row_or_none(
                cast(
                    "object",
                    connection.execute(
                        """SELECT decision_id, plan_id, plan_hash, decided_at, actor, reason
                        FROM plan_decisions
                        WHERE plan_id = ? AND plan_hash = ? AND decision = 'rejected'
                        ORDER BY decided_at, decision_id LIMIT 1""",
                        (plan_id, plan_hash),
                    ).fetchone(),
                )
            )
        if row is None:
            return None
        return RejectionRecord(
            decision_id=_text_column(row, "decision_id"),
            plan_id=_text_column(row, "plan_id"),
            plan_hash=_text_column(row, "plan_hash"),
            decided_at=_aware_datetime_column(row, "decided_at"),
            decided_by=_text_column(row, "actor"),
            reason=_text_column(row, "reason"),
        )

    def get_control_state(self) -> ExecutionControlState:
        """Return the seeded kill-switch row, failing closed when absent."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = _row_or_none(
                cast(
                    "object",
                    connection.execute(
                        """SELECT execution_disabled, changed_at, changed_by, reason, policy_version
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
            policy_version=_text_column(row, "policy_version"),
        )

    def disable(self, state: ExecutionControlState) -> None:
        """Persist a disabled kill-switch state without providing an enable path."""
        if not state.disabled:
            raise ValueError("disable requires a disabled ExecutionControlState.")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor: sqlite3.Cursor = connection.execute(
                """UPDATE execution_control SET execution_disabled = 1,
                changed_at = ?, changed_by = ?, reason = ?, policy_version = ? WHERE control_id = 1""",
                (state.changed_at.isoformat(), state.actor, state.reason or "", state.policy_version),
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
                changed_at = ?, changed_by = ?, reason = ?, policy_version = ? WHERE control_id = 1""",
                (state.changed_at.isoformat(), state.actor, state.reason or "", state.policy_version),
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
                (execution_claim_id, plan_hash, trade_identity, status, claimed_at, updated_at, detail_json)
                VALUES (?, ?, ?, 'claimed', ?, ?, '{}')""",
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

    def nonterminal_claims(self) -> tuple[ExecutionClaim, ...]:
        """Return every claim that may still represent broker exposure."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = list(
                connection.execute(
                    """SELECT plan_hash, trade_identity, status, claimed_at, updated_at, broker_order_id
                    FROM execution_claims
                    WHERE status IN ('claimed', 'submitted', 'partially_filled')
                    ORDER BY claimed_at, plan_hash, trade_identity"""
                ).fetchall()
            )
        return tuple(
            ExecutionClaim(
                plan_hash=_text_column(row, "plan_hash"),
                trade_identity=_text_column(row, "trade_identity"),
                status=_text_column(row, "status"),
                claimed_at=_aware_datetime_column(row, "claimed_at"),
                updated_at=_aware_datetime_column(row, "updated_at"),
                broker_order_id=_optional_text_column(row, "broker_order_id"),
            )
            for row in rows
        )

    def append_event(self, event: ExecutionEvent) -> None:
        """Append one immutable execution event in its own committed transaction."""
        try:
            detail_json: str = json.dumps(
                event.detail,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise ExecutionJournalError("Execution event detail is not canonical JSON.") from error
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """INSERT INTO execution_events
                (event_id, plan_id, plan_hash, trade_identity, client_order_id,
                 phase, occurred_at, broker_order_id, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.plan_id,
                    event.plan_hash,
                    event.trade_identity,
                    event.client_order_id,
                    event.phase.value,
                    event.occurred_at.isoformat(),
                    event.broker_order_id,
                    detail_json,
                ),
            )

    def events_for_plan(self, plan_hash: str) -> tuple[ExecutionEvent, ...]:
        """Return one exact plan's immutable events in deterministic order."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = list(
                connection.execute(
                    """SELECT event_id, plan_id, plan_hash, trade_identity, client_order_id,
                    phase, occurred_at, broker_order_id, detail_json
                    FROM execution_events WHERE plan_hash = ?
                    ORDER BY occurred_at, event_id""",
                    (plan_hash,),
                ).fetchall()
            )
        detail_adapter: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])
        try:
            return tuple(
                ExecutionEvent(
                    event_id=_text_column(row, "event_id"),
                    plan_id=_text_column(row, "plan_id"),
                    plan_hash=_text_column(row, "plan_hash"),
                    trade_identity=_optional_text_column(row, "trade_identity"),
                    client_order_id=_optional_text_column(row, "client_order_id"),
                    phase=ExecutionEventPhase(_text_column(row, "phase")),
                    occurred_at=_aware_datetime_column(row, "occurred_at"),
                    broker_order_id=_optional_text_column(row, "broker_order_id"),
                    detail=detail_adapter.validate_json(_text_column(row, "detail_json")),
                )
                for row in rows
            )
        except (ValidationError, ValueError) as error:
            raise ExecutionJournalError(f"Execution events for plan {plan_hash!r} are invalid.") from error

    def committed_turnover_excluding_plan(self, plan_id: str, as_of: datetime) -> float:
        """Return same-day turnover committed by other submitted plans."""
        market_timezone = ZoneInfo("America/New_York")
        trading_date = as_of.astimezone(market_timezone).date()
        day_start = datetime.combine(trading_date, datetime_time.min, tzinfo=market_timezone).astimezone(UTC)
        day_end = datetime.combine(
            trading_date + timedelta(days=1),
            datetime_time.min,
            tzinfo=market_timezone,
        ).astimezone(UTC)
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT DISTINCT p.payload_json FROM execution_events AS e
                    JOIN portfolio_plans AS p ON p.plan_id = e.plan_id
                    WHERE e.plan_id <> ? AND e.phase IN ('submitted', 'partially_filled', 'filled')
                    AND e.occurred_at >= ? AND e.occurred_at < ?""",
                    (plan_id, day_start.isoformat(), day_end.isoformat()),
                ).fetchall(),
            )
        try:
            return sum(
                PortfolioPlan.model_validate_json(_text_column(row, "payload_json")).payload.turnover_estimate
                for row in rows
            )
        except ValidationError as error:
            raise PersistedPlanIntegrityError("Committed-turnover plan is invalid.") from error


class TurnoverReservationRepository:
    """Reserve daily turnover atomically before any broker-side effect."""

    def __init__(self, database: Database) -> None:
        """Bind reservations to the shared BEGIN IMMEDIATE transaction authority."""
        self._database: Database = database

    def reserve_turnover(
        self,
        *,
        account_id: str,
        trading_date: date,
        plan_id: str,
        plan_hash: str,
        amount_fraction: float,
        maximum_fraction: float,
        reserved_at: datetime,
    ) -> TurnoverReservation:
        """Atomically reserve capacity or fail without changing aggregate commitment."""
        reservation = TurnoverReservation(
            reservation_id=_turnover_reservation_id(account_id, trading_date, plan_hash),
            account_id=account_id,
            trading_date=trading_date,
            plan_id=plan_id,
            plan_hash=plan_hash,
            amount_fraction=amount_fraction,
            maximum_fraction=maximum_fraction,
            status=TurnoverReservationStatus.RESERVED,
            reserved_at=reserved_at,
        )
        with self._database.transaction(TransactionMode.WRITE) as connection:
            plan_row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT plan_hash FROM portfolio_plans WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone(),
            )
            if plan_row is None or _text_column(plan_row, "plan_hash") != plan_hash:
                raise TurnoverReservationConflictError("turnover reservation must bind an exact durable portfolio plan")
            existing = _select_turnover_reservation(connection, reservation.reservation_id)
            if existing is not None:
                immutable_existing = existing.model_copy(update={"status": reservation.status, "terminal_at": None})
                if immutable_existing != reservation:
                    raise TurnoverReservationConflictError("turnover reservation identity is bound to different terms")
                if existing.status is TurnoverReservationStatus.RELEASED:
                    raise TurnoverReservationConflictError(
                        "released turnover reservation cannot authorize a later submission"
                    )
                return existing
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """SELECT COALESCE(SUM(amount_fraction), 0.0) AS committed_fraction
                    FROM turnover_reservations
                    WHERE account_id = ? AND trading_date = ?
                      AND status IN ('reserved', 'settled')""",
                    (account_id, trading_date.isoformat()),
                ).fetchone(),
            )
            committed = 0.0 if row is None else _float_column(row, "committed_fraction")
            if committed + amount_fraction > maximum_fraction:
                raise TurnoverCapExceededError("daily turnover reservation would exceed the configured maximum")
            _insert_turnover_reservation(connection, reservation)
        return reservation

    def settle(self, reservation_id: str, *, settled_at: datetime) -> TurnoverReservation:
        """Mark reserved turnover committed after confirmed broker submission."""
        return self._transition(
            reservation_id,
            status=TurnoverReservationStatus.SETTLED,
            terminal_at=settled_at,
        )

    def release(self, reservation_id: str, *, released_at: datetime) -> TurnoverReservation:
        """Release capacity only when broker submission did not occur."""
        return self._transition(
            reservation_id,
            status=TurnoverReservationStatus.RELEASED,
            terminal_at=released_at,
        )

    def _transition(
        self,
        reservation_id: str,
        *,
        status: TurnoverReservationStatus,
        terminal_at: datetime,
    ) -> TurnoverReservation:
        with self._database.transaction(TransactionMode.WRITE) as connection:
            existing = _select_turnover_reservation(connection, reservation_id)
            if existing is None:
                raise TurnoverReservationConflictError("turnover reservation does not exist")
            if existing.status is status:
                if existing.terminal_at != terminal_at:
                    raise TurnoverReservationConflictError("turnover reservation already has another terminal time")
                return existing
            if existing.status is not TurnoverReservationStatus.RESERVED:
                raise TurnoverReservationConflictError("terminal turnover reservation cannot change state")
            transitioned = existing.model_copy(update={"status": status, "terminal_at": terminal_at})
            transitioned = TurnoverReservation.model_validate(transitioned)
            cursor = connection.execute(
                """UPDATE turnover_reservations
                SET status = ?, terminal_at = ?, reservation_json = ?
                WHERE reservation_id = ? AND status = 'reserved'""",
                (
                    transitioned.status.value,
                    terminal_at.isoformat(),
                    transitioned.model_dump_json(),
                    reservation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise TurnoverReservationConflictError("turnover reservation changed during transition")
        return transitioned


def _turnover_reservation_id(account_id: str, trading_date: date, plan_hash: str) -> str:
    encoded = f"{account_id}\0{trading_date.isoformat()}\0{plan_hash}".encode()
    return f"turnover:{hashlib.sha256(encoded).hexdigest()}"


def _insert_turnover_reservation(
    connection: sqlite3.Connection,
    reservation: TurnoverReservation,
) -> None:
    _ = connection.execute(
        """INSERT INTO turnover_reservations (
            reservation_id, account_id, trading_date, plan_id, plan_hash,
            amount_fraction, maximum_fraction, status, reserved_at, terminal_at,
            reservation_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            reservation.reservation_id,
            reservation.account_id,
            reservation.trading_date.isoformat(),
            reservation.plan_id,
            reservation.plan_hash,
            reservation.amount_fraction,
            reservation.maximum_fraction,
            reservation.status.value,
            reservation.reserved_at.isoformat(),
            None,
            reservation.model_dump_json(),
        ),
    )


def _select_turnover_reservation(
    connection: sqlite3.Connection,
    reservation_id: str,
) -> TurnoverReservation | None:
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute(
            "SELECT reservation_json FROM turnover_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone(),
    )
    if row is None:
        return None
    try:
        return TurnoverReservation.model_validate_json(_text_column(row, "reservation_json"))
    except ValidationError as error:
        raise TurnoverReservationConflictError("stored turnover reservation is invalid") from error


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
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ExecutionControlError(f"Stored {name} must be text or null.")
    return value


def _float_column(row: sqlite3.Row, name: str) -> float:
    value: object = _column(row, name)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise ExecutionControlError(f"Column {name!r} is not a real number.")


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
