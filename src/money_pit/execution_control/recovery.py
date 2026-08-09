"""Module containing durable broker reconciliation for interrupted executions."""

import uuid
from collections.abc import Callable
from datetime import datetime

from money_pit.execution_control.errors import BrokerObservationUnavailableError
from money_pit.execution_control.errors import ExecutionJournalError
from money_pit.execution_control.fills import ExecutionPhase
from money_pit.execution_control.fills import FillObservation
from money_pit.execution_control.fills import is_terminal_status
from money_pit.execution_control.models import ExecutionEvent
from money_pit.execution_control.models import ExecutionEventPhase
from money_pit.execution_control.models import RecoveryResult
from money_pit.execution_control.protocols import ExecutionClaimRepository
from money_pit.execution_control.protocols import ExecutionJournalRepository


Clock = Callable[[], datetime]
EventIdFactory = Callable[[], str]
FillObserver = Callable[[str], FillObservation]


def _recovery_phase(observation: FillObservation) -> tuple[str, ExecutionEventPhase]:
    if observation.phase is ExecutionPhase.FILLED:
        return "filled", ExecutionEventPhase.FILLED
    if observation.phase is ExecutionPhase.PARTIALLY_FILLED:
        return "partially_filled", ExecutionEventPhase.PARTIALLY_FILLED
    if observation.phase is ExecutionPhase.REJECTED:
        return "failed", ExecutionEventPhase.REJECTED
    if is_terminal_status(observation.status):
        return "cancelled", ExecutionEventPhase.CANCELLED
    return "submitted", ExecutionEventPhase.RECOVERY_HALTED


def _plan_id(journal: ExecutionJournalRepository, plan_hash: str) -> str:
    events: tuple[ExecutionEvent, ...] = journal.events_for_plan(plan_hash)
    if not events:
        raise ExecutionJournalError(f"Execution claim for plan {plan_hash!r} has no durable intent event.")
    return events[0].plan_id


def reconcile_nonterminal_claims(
    claims: ExecutionClaimRepository,
    journal: ExecutionJournalRepository,
    observe_fill: FillObserver,
    *,
    clock: Clock,
    event_id_factory: EventIdFactory = lambda: str(uuid.uuid4()),
) -> RecoveryResult:
    """Re-observe every nonterminal claim and halt while any order remains uncertain."""
    checked_at: datetime = clock()
    reconciled: list[str] = []
    unresolved: list[str] = []
    for claim in claims.nonterminal_claims():
        plan_id: str = _plan_id(journal, claim.plan_hash)
        try:
            observation: FillObservation = observe_fill(claim.trade_identity)
        except BrokerObservationUnavailableError as error:
            unresolved.append(claim.trade_identity)
            journal.append_event(
                ExecutionEvent(
                    event_id=event_id_factory(),
                    plan_id=plan_id,
                    plan_hash=claim.plan_hash,
                    trade_identity=claim.trade_identity,
                    client_order_id=claim.trade_identity,
                    phase=ExecutionEventPhase.RECOVERY_HALTED,
                    occurred_at=checked_at,
                    broker_order_id=claim.broker_order_id,
                    detail={"observation_error": type(error).__name__},
                )
            )
            continue
        status, event_phase = _recovery_phase(observation)
        claims.update(
            claim.plan_hash,
            claim.trade_identity,
            status=status,
            updated_at=checked_at,
            broker_order_id=claim.broker_order_id,
        )
        journal.append_event(
            ExecutionEvent(
                event_id=event_id_factory(),
                plan_id=plan_id,
                plan_hash=claim.plan_hash,
                trade_identity=claim.trade_identity,
                client_order_id=claim.trade_identity,
                phase=event_phase,
                occurred_at=checked_at,
                broker_order_id=claim.broker_order_id,
                detail={
                    "observed_status": observation.status,
                    "filled_qty": observation.filled_qty,
                    "filled_avg_price": observation.filled_avg_price,
                    "realized_notional": observation.realized_notional,
                },
            )
        )
        if event_phase is ExecutionEventPhase.RECOVERY_HALTED:
            unresolved.append(claim.trade_identity)
        else:
            reconciled.append(claim.trade_identity)
    return RecoveryResult(
        checked_at=checked_at,
        reconciled_trade_identities=tuple(reconciled),
        unresolved_trade_identities=tuple(unresolved),
    )
