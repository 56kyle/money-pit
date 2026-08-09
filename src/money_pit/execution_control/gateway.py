"""Module containing the sole capital-write gateway for A6 execution."""

import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from money_pit.execution_control.errors import BrokerObservationUnavailableError
from money_pit.execution_control.errors import BrokerSubmissionError
from money_pit.execution_control.errors import BrokerWriteUnavailableError
from money_pit.execution_control.errors import PlanNotFoundError
from money_pit.execution_control.errors import PreflightDeniedError
from money_pit.execution_control.errors import RecoveryRequiredError
from money_pit.execution_control.fills import ExecutionPhase
from money_pit.execution_control.fills import FillObservation
from money_pit.execution_control.fills import is_terminal_status
from money_pit.execution_control.models import ConfirmedFill
from money_pit.execution_control.models import ExecutionEvent
from money_pit.execution_control.models import ExecutionEventPhase
from money_pit.execution_control.models import ExecutionReceipt
from money_pit.execution_control.models import PreflightDenial
from money_pit.execution_control.models import PreflightDenialCode
from money_pit.execution_control.models import PreflightResult
from money_pit.execution_control.models import TurnoverReservation
from money_pit.execution_control.orders import OrderIntent
from money_pit.execution_control.orders import execution_parameters_for_trade
from money_pit.execution_control.preflight import validate_pre_execution
from money_pit.execution_control.protocols import AutonomousEligibilityEvaluator
from money_pit.execution_control.protocols import DecisionRepository
from money_pit.execution_control.protocols import ExecutionClaimRepository
from money_pit.execution_control.protocols import ExecutionJournalRepository
from money_pit.execution_control.protocols import KillSwitchStore
from money_pit.execution_control.protocols import PlanRepository
from money_pit.execution_control.repository import TurnoverCapExceededError
from money_pit.execution_control.repository import TurnoverReservationConflictError
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan


OrderPlacer = Callable[[OrderIntent], str]
OrderPlacerFactory = Callable[[BrokerEnvironment], OrderPlacer]
Clock = Callable[[], datetime]
EventIdFactory = Callable[[], str]
FillObserver = Callable[[str], FillObservation]
Sleeper = Callable[[float], None]
MonotonicClock = Callable[[], float]
CommittedTurnoverReader = Callable[[str, datetime], float]
CurrentStateCheck = Callable[[PortfolioPlan, tuple[ConfirmedFill, ...]], bool]


class TurnoverReservationStore(Protocol):
    """Atomic account-day turnover capacity boundary."""

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
        """Reserve exact account-day capacity atomically."""
        ...

    def settle(self, reservation_id: str, *, settled_at: datetime) -> TurnoverReservation:
        """Commit a reservation after confirmed broker submission."""
        ...

    def release(self, reservation_id: str, *, released_at: datetime) -> TurnoverReservation:
        """Release capacity when no broker submission occurred."""
        ...


@dataclass(frozen=True)
class CurrentStateChecks:
    """All trusted reads that must be fresh immediately before a capital write."""

    portfolio_unchanged: CurrentStateCheck
    market_unchanged: CurrentStateCheck
    cash_unchanged: CurrentStateCheck
    open_orders_unchanged: CurrentStateCheck
    evidence_fresh: CurrentStateCheck
    tax_state_unchanged: CurrentStateCheck
    account_ready: CurrentStateCheck
    market_fresh: CurrentStateCheck


@dataclass(frozen=True)
class ExecutionGatewayDependencies:
    """Scoped dependencies available only to the explicit A6 execute operation."""

    plans: PlanRepository
    decisions: DecisionRepository
    kill_switch: KillSwitchStore
    claims: ExecutionClaimRepository
    journal: ExecutionJournalRepository
    current_state: CurrentStateChecks
    execution_config_hash: str
    committed_turnover_excluding_plan: CommittedTurnoverReader
    turnover_reservations: TurnoverReservationStore
    order_placer_factory: OrderPlacerFactory
    observe_fill: FillObserver
    clock: Clock
    event_id_factory: EventIdFactory = lambda: str(uuid.uuid4())
    autonomous_eligibility: AutonomousEligibilityEvaluator | None = None
    poll_interval_seconds: float = 1.0
    poll_timeout_seconds: float = 30.0
    sleep: Sleeper = time.sleep
    monotonic: MonotonicClock = time.monotonic

    def __post_init__(self) -> None:
        """Reject polling settings that cannot make bounded progress."""
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive.")
        if self.poll_timeout_seconds <= 0:
            raise ValueError("poll_timeout_seconds must be positive.")


def _state_denial(
    plan: PortfolioPlan,
    confirmed_fills: tuple[ConfirmedFill, ...],
    check: CurrentStateCheck,
    code: PreflightDenialCode,
    label: str,
) -> PreflightDenial | None:
    try:
        current: bool = check(plan, confirmed_fills)
    except Exception as error:  # noqa: BLE001 - normalize every provider/storage boundary failure.
        return PreflightDenial(code=code, detail=f"{label} could not be revalidated: {type(error).__name__}.")
    if current:
        return None
    return PreflightDenial(code=code, detail=f"{label} differs from the plan-bound state.")


def revalidate_plan(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    dependencies: ExecutionGatewayDependencies,
    *,
    now: datetime,
    confirmed_fills: tuple[ConfirmedFill, ...] = (),
) -> PreflightResult:
    """Revalidate every authority, freshness, portfolio, order, cash, and tax gate."""
    try:
        base: PreflightResult = validate_pre_execution(
            plan,
            policy,
            dependencies.decisions,
            dependencies.kill_switch,
            now=now,
            portfolio_unchanged=lambda candidate: dependencies.current_state.portfolio_unchanged(
                candidate, confirmed_fills
            ),
            market_unchanged=lambda candidate: dependencies.current_state.market_unchanged(candidate, confirmed_fills),
            autonomous_eligibility=dependencies.autonomous_eligibility,
            execution_config_hash=dependencies.execution_config_hash,
            committed_turnover=dependencies.committed_turnover_excluding_plan(plan.payload.plan_id, now),
        )
    except Exception as error:  # noqa: BLE001 - normalize every provider/storage boundary failure.
        return PreflightResult(
            plan_id=plan.payload.plan_id,
            plan_hash=plan.plan_hash,
            checked_at=now,
            denials=(
                PreflightDenial(
                    code=PreflightDenialCode.CURRENT_STATE_UNAVAILABLE,
                    detail=f"Execution authority or current state is unavailable: {type(error).__name__}.",
                ),
            ),
        )
    checks: tuple[tuple[CurrentStateCheck, PreflightDenialCode, str], ...] = (
        (dependencies.current_state.cash_unchanged, PreflightDenialCode.CASH_STATE_DRIFT, "Cash state"),
        (
            dependencies.current_state.open_orders_unchanged,
            PreflightDenialCode.OPEN_ORDERS_STATE_DRIFT,
            "Open-order state",
        ),
        (dependencies.current_state.evidence_fresh, PreflightDenialCode.EVIDENCE_STALE, "Evidence freshness"),
        (dependencies.current_state.tax_state_unchanged, PreflightDenialCode.TAX_STATE_DRIFT, "Tax state"),
        (
            dependencies.current_state.account_ready,
            PreflightDenialCode.BROKER_ACCOUNT_BLOCKED,
            "Broker account readiness",
        ),
        (dependencies.current_state.market_fresh, PreflightDenialCode.MARKET_STATE_DRIFT, "Market quote freshness"),
    )
    extra: tuple[PreflightDenial, ...] = tuple(
        denial
        for check, code, label in checks
        if (denial := _state_denial(plan, confirmed_fills, check, code, label)) is not None
    )
    return base.model_copy(update={"denials": (*base.denials, *extra)})


def _event(
    dependencies: ExecutionGatewayDependencies,
    plan: PortfolioPlan,
    *,
    trade_identity: str,
    phase: ExecutionEventPhase,
    occurred_at: datetime,
    broker_order_id: str | None = None,
    detail: dict[str, object] | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=dependencies.event_id_factory(),
        plan_id=plan.payload.plan_id,
        plan_hash=plan.plan_hash,
        trade_identity=trade_identity,
        client_order_id=trade_identity,
        phase=phase,
        occurred_at=occurred_at,
        broker_order_id=broker_order_id,
        detail={} if detail is None else detail,
    )


def _observation_state(observation: FillObservation) -> tuple[str, ExecutionEventPhase, bool]:
    if observation.phase is ExecutionPhase.FILLED:
        return "filled", ExecutionEventPhase.FILLED, True
    if observation.phase is ExecutionPhase.PARTIALLY_FILLED:
        return "partially_filled", ExecutionEventPhase.PARTIALLY_FILLED, False
    if observation.phase is ExecutionPhase.REJECTED:
        return "failed", ExecutionEventPhase.REJECTED, True
    if is_terminal_status(observation.status):
        return "cancelled", ExecutionEventPhase.CANCELLED, True
    return "submitted", ExecutionEventPhase.RECOVERY_OBSERVED, False


def _observe_until_terminal(
    dependencies: ExecutionGatewayDependencies,
    plan: PortfolioPlan,
    *,
    trade_identity: str,
    broker_order_id: str,
) -> tuple[ExecutionEventPhase, FillObservation] | None:
    """Observe and persist distinct fill transitions until terminal state or timeout."""
    deadline: float = dependencies.monotonic() + dependencies.poll_timeout_seconds
    last_state: tuple[str, str, float | None] | None = None
    while True:
        observation: FillObservation | None = None
        with suppress(BrokerObservationUnavailableError):
            observation = dependencies.observe_fill(trade_identity)
        if observation is not None:
            status, event_phase, terminal = _observation_state(observation)
            observed_state: tuple[str, str, float | None] = (
                status,
                observation.status,
                observation.filled_qty,
            )
            if observed_state != last_state:
                observed_at: datetime = dependencies.clock()
                dependencies.claims.update(
                    plan.plan_hash,
                    trade_identity,
                    status=status,
                    updated_at=observed_at,
                    broker_order_id=broker_order_id,
                )
                dependencies.journal.append_event(
                    _event(
                        dependencies,
                        plan,
                        trade_identity=trade_identity,
                        phase=event_phase,
                        occurred_at=observed_at,
                        broker_order_id=broker_order_id,
                        detail={
                            "observed_status": observation.status,
                            "filled_qty": observation.filled_qty,
                            "filled_avg_price": observation.filled_avg_price,
                            "realized_notional": observation.realized_notional,
                        },
                    )
                )
                last_state = observed_state
            if terminal:
                return event_phase, observation
        if dependencies.monotonic() >= deadline:
            dependencies.journal.append_event(
                _event(
                    dependencies,
                    plan,
                    trade_identity=trade_identity,
                    phase=ExecutionEventPhase.RECOVERY_HALTED,
                    occurred_at=dependencies.clock(),
                    broker_order_id=broker_order_id,
                    detail={"reason": "fill_observation_timeout"},
                )
            )
            return None
        dependencies.sleep(dependencies.poll_interval_seconds)


def _confirmed_fill(
    *,
    trade_identity: str,
    instrument: str,
    side: str,
    observation: FillObservation,
) -> ConfirmedFill:
    """Return complete fill economics for expected-state revalidation."""
    if observation.filled_qty is None or observation.filled_avg_price is None or observation.realized_notional is None:
        raise RecoveryRequiredError(
            f"Filled order {trade_identity!r} lacks complete fill economics; reconciliation is required."
        )
    return ConfirmedFill(
        trade_identity=trade_identity,
        instrument=instrument,
        side=side,
        filled_qty=observation.filled_qty,
        filled_avg_price=observation.filled_avg_price,
        realized_notional=observation.realized_notional,
    )


def _reserve_daily_turnover(
    plan: PortfolioPlan,
    policy: ExecutionPolicy,
    dependencies: ExecutionGatewayDependencies,
    *,
    started_at: datetime,
    preflight: PreflightResult,
) -> TurnoverReservation:
    trading_date = started_at.astimezone(ZoneInfo("America/New_York")).date()
    try:
        return dependencies.turnover_reservations.reserve_turnover(
            account_id=plan.payload.account_id,
            trading_date=trading_date,
            plan_id=plan.payload.plan_id,
            plan_hash=plan.plan_hash,
            amount_fraction=plan.payload.turnover_estimate,
            maximum_fraction=policy.maximum_daily_turnover,
            reserved_at=started_at,
        )
    except (TurnoverCapExceededError, TurnoverReservationConflictError):
        denial = PreflightDenial(
            code=PreflightDenialCode.CONSTRAINT_FAILED,
            detail="Atomic daily-turnover capacity could not be reserved.",
        )
        raise PreflightDeniedError(preflight.model_copy(update={"denials": (*preflight.denials, denial)})) from None
    except Exception as error:  # noqa: BLE001 - normalize the durable reservation boundary.
        denial = PreflightDenial(
            code=PreflightDenialCode.CURRENT_STATE_UNAVAILABLE,
            detail=f"Daily-turnover state is unavailable: {type(error).__name__}.",
        )
        raise PreflightDeniedError(preflight.model_copy(update={"denials": (*preflight.denials, denial)})) from None


def _construct_order_placer(
    policy: ExecutionPolicy,
    dependencies: ExecutionGatewayDependencies,
    reservation: TurnoverReservation,
) -> OrderPlacer:
    try:
        return dependencies.order_placer_factory(policy.broker_environment)
    except Exception as error:
        _ = dependencies.turnover_reservations.release(reservation.reservation_id, released_at=dependencies.clock())
        raise BrokerWriteUnavailableError("The scoped A6 broker writer could not be constructed.") from error


def _require_ready_plan(
    plan_id: str,
    policy: ExecutionPolicy,
    dependencies: ExecutionGatewayDependencies,
) -> tuple[PortfolioPlan, datetime, PreflightResult]:
    plan = dependencies.plans.get(plan_id)
    if plan is None:
        raise PlanNotFoundError(f"Portfolio plan {plan_id!r} does not exist.")
    if dependencies.claims.nonterminal_claims():
        raise RecoveryRequiredError("Nonterminal execution claims must be reconciled before a new plan executes.")
    started_at = dependencies.clock()
    initial = revalidate_plan(plan, policy, dependencies, now=started_at)
    if not initial.allowed:
        raise PreflightDeniedError(initial)
    return plan, started_at, initial


def execute_plan(
    plan_id: str,
    policy: ExecutionPolicy,
    dependencies: ExecutionGatewayDependencies,
) -> ExecutionReceipt:
    """Execute one durable exact-hash plan through the only broker-write boundary."""
    plan, started_at, initial = _require_ready_plan(plan_id, policy, dependencies)
    if not plan.payload.proposed_trades:
        finished_at = dependencies.clock()
        return ExecutionReceipt(
            plan_id=plan.payload.plan_id,
            plan_hash=plan.plan_hash,
            started_at=started_at,
            finished_at=finished_at,
            submitted_trade_identities=(),
            completed_trade_identities=(),
            halted=False,
        )
    reservation = _reserve_daily_turnover(plan, policy, dependencies, started_at=started_at, preflight=initial)
    place_order = _construct_order_placer(policy, dependencies, reservation)

    submitted: list[str] = []
    completed: list[str] = []
    confirmed_fills: list[ConfirmedFill] = []
    for trade_index, trade in enumerate(plan.payload.proposed_trades):
        checked_at: datetime = dependencies.clock()
        result: PreflightResult = revalidate_plan(
            plan,
            policy,
            dependencies,
            now=checked_at,
            confirmed_fills=tuple(confirmed_fills),
        )
        if not result.allowed:
            if not submitted:
                _ = dependencies.turnover_reservations.release(
                    reservation.reservation_id, released_at=dependencies.clock()
                )
            raise PreflightDeniedError(result)
        parameters: OrderIntent = execution_parameters_for_trade(plan, trade_index, trade)
        trade_identity: str = parameters.client_order_id
        dependencies.journal.append_event(
            _event(
                dependencies,
                plan,
                trade_identity=trade_identity,
                phase=ExecutionEventPhase.INTENT_RECORDED,
                occurred_at=checked_at,
                detail={"order": parameters.to_order_payload()},
            )
        )
        dependencies.claims.claim(plan.plan_hash, trade_identity, claimed_at=checked_at)
        dependencies.journal.append_event(
            _event(
                dependencies,
                plan,
                trade_identity=trade_identity,
                phase=ExecutionEventPhase.CLAIMED,
                occurred_at=checked_at,
            )
        )
        try:
            broker_order_id: str = place_order(parameters)
        except Exception as error:
            dependencies.journal.append_event(
                _event(
                    dependencies,
                    plan,
                    trade_identity=trade_identity,
                    phase=ExecutionEventPhase.FAILED,
                    occurred_at=dependencies.clock(),
                    detail={"submission_status": "unknown"},
                )
            )
            raise BrokerSubmissionError(
                f"Order {trade_identity!r} could not be confirmed; reconciliation is required."
            ) from error
        if not submitted:
            _ = dependencies.turnover_reservations.settle(reservation.reservation_id, settled_at=dependencies.clock())
        submitted_at: datetime = dependencies.clock()
        dependencies.claims.update(
            plan.plan_hash,
            trade_identity,
            status="submitted",
            updated_at=submitted_at,
            broker_order_id=broker_order_id,
        )
        dependencies.journal.append_event(
            _event(
                dependencies,
                plan,
                trade_identity=trade_identity,
                phase=ExecutionEventPhase.SUBMITTED,
                occurred_at=submitted_at,
                broker_order_id=broker_order_id,
            )
        )
        submitted.append(trade_identity)
        terminal_result: tuple[ExecutionEventPhase, FillObservation] | None = _observe_until_terminal(
            dependencies,
            plan,
            trade_identity=trade_identity,
            broker_order_id=broker_order_id,
        )
        if terminal_result is None:
            raise RecoveryRequiredError(
                f"Order {trade_identity!r} did not reach a confirmed filled state; reconciliation is required."
            )
        terminal_phase, observation = terminal_result
        if terminal_phase is not ExecutionEventPhase.FILLED:
            raise BrokerSubmissionError(
                f"Order {trade_identity!r} reached terminal phase {terminal_phase.value!r} without filling."
            )
        completed.append(trade_identity)
        confirmed_fills.append(
            _confirmed_fill(
                trade_identity=trade_identity,
                instrument=trade.instrument,
                side=trade.side,
                observation=observation,
            )
        )

    finished_at: datetime = dependencies.clock()
    return ExecutionReceipt(
        plan_id=plan.payload.plan_id,
        plan_hash=plan.plan_hash,
        started_at=started_at,
        finished_at=finished_at,
        submitted_trade_identities=tuple(submitted),
        completed_trade_identities=tuple(completed),
        halted=False,
    )
