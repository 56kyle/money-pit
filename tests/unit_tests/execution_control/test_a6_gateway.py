"""Tests for the sole A6 capital-write gateway."""

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta

import pytest
from typing_extensions import override

from money_pit.execution_control.errors import BrokerObservationUnavailableError
from money_pit.execution_control.errors import BrokerSubmissionError
from money_pit.execution_control.errors import PreflightDeniedError
from money_pit.execution_control.errors import RecoveryRequiredError
from money_pit.execution_control.fills import ExecutionPhase
from money_pit.execution_control.fills import FillObservation
from money_pit.execution_control.gateway import CurrentStateChecks
from money_pit.execution_control.gateway import ExecutionGatewayDependencies
from money_pit.execution_control.gateway import _reserve_daily_turnover  # pyright: ignore[reportPrivateUsage]
from money_pit.execution_control.gateway import execute_plan
from money_pit.execution_control.models import ConfirmedFill
from money_pit.execution_control.models import ExecutionClaim
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.models import ExecutionEvent
from money_pit.execution_control.models import ExecutionEventPhase
from money_pit.execution_control.models import PreflightResult
from money_pit.execution_control.models import TurnoverReservation
from money_pit.execution_control.models import TurnoverReservationStatus
from money_pit.execution_control.orders import OrderIntent
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import PlanDecision
from money_pit.plans.lifecycle import RejectionRecord
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.portfolio_plan import PlanTaxEstimate
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade


_NOW = datetime(2026, 8, 9, 14, 0, tzinfo=UTC)
_SNAPSHOT_HASH = "1" * 64


def _plan(*, trades: tuple[ProposedTrade, ...] | None = None) -> PortfolioPlan:
    selected_trades = trades or (
        ProposedTrade(
            instrument="AAPL",
            asset_class=TradableAssetClass.US_EQUITY,
            side="buy",
            quantity=2.0,
            estimated_notional=400.0,
            tax_cost_known=True,
        ),
    )
    return PortfolioPlan.from_payload(
        PortfolioPlanPayload(
            plan_id="plan-a6",
            created_at=_NOW - timedelta(minutes=1),
            expires_at=_NOW + timedelta(minutes=30),
            portfolio_snapshot_id="portfolio-1",
            market_snapshot_id="market-1",
            decision_snapshot_id="decision-1",
            decision_snapshot_hash=_SNAPSHOT_HASH,
            account_id="paper-account",
            broker_environment=BrokerEnvironment.PAPER,
            policy_version="policy-1",
            target_weights={"AAPL": 0.2, "MSFT": 0.2},
            proposed_trades=selected_trades,
            turnover_estimate=0.1,
            tax_estimate=PlanTaxEstimate(currency="USD", estimated_cost=0.0, known=True),
            evidence_gate_results={"verified": True},
            constraint_results={"bounded": True},
        )
    )


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        maximum_order_notional=1_000.0,
        maximum_daily_turnover=0.2,
    )


@dataclass
class _Plans:
    plan: PortfolioPlan

    def get(self, plan_id: str) -> PortfolioPlan | None:
        return self.plan if plan_id == self.plan.payload.plan_id else None


@dataclass
class _Decisions:
    approval: ApprovalRecord

    def append(self, record: PlanDecision) -> None:
        del record
        raise AssertionError("execution must not add plan decisions")

    def latest_for(self, plan_id: str) -> PlanDecision | None:
        del plan_id
        return self.approval

    def rejection_for(self, plan_id: str, plan_hash: str) -> RejectionRecord | None:
        del plan_id, plan_hash
        return None


@dataclass
class _KillSwitch:
    state: ExecutionControlState

    def get_control_state(self) -> ExecutionControlState:
        return self.state

    def disable(self, state: ExecutionControlState) -> None:
        self.state = state

    def enable(self, state: ExecutionControlState) -> None:
        self.state = state


@dataclass
class _Claims:
    trace: list[str] = field(default_factory=list)
    unresolved: tuple[ExecutionClaim, ...] = ()

    def claim(self, plan_hash: str, trade_identity: str, *, claimed_at: datetime) -> None:
        del plan_hash, claimed_at
        self.trace.append(f"claim:{trade_identity}")

    def update(
        self,
        plan_hash: str,
        trade_identity: str,
        *,
        status: str,
        updated_at: datetime,
        broker_order_id: str | None,
    ) -> None:
        del plan_hash, updated_at
        self.trace.append(f"update:{trade_identity}:{status}:{broker_order_id}")

    def nonterminal_claims(self) -> tuple[ExecutionClaim, ...]:
        return self.unresolved


@dataclass
class _Journal:
    trace: list[str] = field(default_factory=list)
    events: list[ExecutionEvent] = field(default_factory=list)

    def append_event(self, event: ExecutionEvent) -> None:
        self.events.append(event)
        self.trace.append(f"event:{event.phase.value}:{event.trade_identity}")

    def events_for_plan(self, plan_hash: str) -> tuple[ExecutionEvent, ...]:
        return tuple(event for event in self.events if event.plan_hash == plan_hash)


@dataclass
class _TurnoverReservations:
    reservation: TurnoverReservation | None = None

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
        self.reservation = TurnoverReservation(
            reservation_id=f"turnover:{plan_hash}",
            account_id=account_id,
            trading_date=trading_date,
            plan_id=plan_id,
            plan_hash=plan_hash,
            amount_fraction=amount_fraction,
            maximum_fraction=maximum_fraction,
            status=TurnoverReservationStatus.RESERVED,
            reserved_at=reserved_at,
        )
        return self.reservation

    def settle(self, reservation_id: str, *, settled_at: datetime) -> TurnoverReservation:
        assert self.reservation is not None
        assert self.reservation.reservation_id == reservation_id
        self.reservation = self.reservation.model_copy(
            update={"status": TurnoverReservationStatus.SETTLED, "terminal_at": settled_at}
        )
        return self.reservation

    def release(self, reservation_id: str, *, released_at: datetime) -> TurnoverReservation:
        assert self.reservation is not None
        assert self.reservation.reservation_id == reservation_id
        self.reservation = self.reservation.model_copy(
            update={"status": TurnoverReservationStatus.RELEASED, "terminal_at": released_at}
        )
        return self.reservation


def _dependencies(
    plan: PortfolioPlan,
    *,
    claims: _Claims | None = None,
    journal: _Journal | None = None,
    checks: CurrentStateChecks | None = None,
    factory: Callable[[BrokerEnvironment], Callable[[OrderIntent], str]] | None = None,
    observe_fill: Callable[[str], FillObservation] | None = None,
    monotonic: Callable[[], float] | None = None,
    turnover_reservations: _TurnoverReservations | None = None,
) -> ExecutionGatewayDependencies:
    approval = ApprovalRecord(
        decision_id="approval-1",
        plan_id=plan.payload.plan_id,
        plan_hash=plan.plan_hash,
        decided_at=_NOW,
        decided_by="operator",
        execution_config_hash="e" * 64,
        execution_policy_hash=_policy().fingerprint(),
        execution_policy_version=_policy().policy_version,
        broker_environment=_policy().broker_environment,
        account_id=plan.payload.account_id,
        committed_turnover_at_approval=0.0,
    )

    def unchanged(_plan: PortfolioPlan, _confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        return True

    return ExecutionGatewayDependencies(
        plans=_Plans(plan),
        decisions=_Decisions(approval),
        kill_switch=_KillSwitch(
            ExecutionControlState(
                disabled=False,
                changed_at=_NOW,
                actor="operator",
                policy_version="policy-1",
            )
        ),
        claims=claims or _Claims(),
        journal=journal or _Journal(),
        current_state=checks
        or CurrentStateChecks(
            portfolio_unchanged=unchanged,
            market_unchanged=unchanged,
            cash_unchanged=unchanged,
            open_orders_unchanged=unchanged,
            evidence_fresh=unchanged,
            tax_state_unchanged=unchanged,
            account_ready=unchanged,
            market_fresh=unchanged,
        ),
        execution_config_hash="e" * 64,
        committed_turnover_excluding_plan=lambda _plan_id, _as_of: 0.0,
        turnover_reservations=turnover_reservations or _TurnoverReservations(),
        order_placer_factory=factory or (lambda _environment: lambda _parameters: "broker-1"),
        observe_fill=observe_fill
        or (
            lambda _identity: FillObservation(
                phase=ExecutionPhase.FILLED,
                status="filled",
                filled_qty=1,
                filled_avg_price=100,
                realized_notional=100,
            )
        ),
        clock=lambda: _NOW,
        event_id_factory=lambda: "event-1",
        poll_interval_seconds=0.01,
        poll_timeout_seconds=2,
        sleep=lambda _seconds: None,
        monotonic=monotonic or (lambda: 0),
    )


def test__reserve_daily_turnover_uses_eastern_trading_date() -> None:
    plan = _plan()
    reservations = _TurnoverReservations()
    dependencies = _dependencies(plan, turnover_reservations=reservations)
    started_at = datetime(2026, 8, 10, 2, tzinfo=UTC)

    _ = _reserve_daily_turnover(
        plan,
        _policy(),
        dependencies,
        started_at=started_at,
        preflight=PreflightResult(
            plan_id=plan.payload.plan_id,
            plan_hash=plan.plan_hash,
            checked_at=started_at,
            denials=(),
        ),
    )

    assert reservations.reservation is not None
    assert reservations.reservation.trading_date == date(2026, 8, 9)


def test_execute_plan_does_not_construct_writer_when_preflight_denies() -> None:
    plan = _plan()
    constructed: list[BrokerEnvironment] = []

    def factory(environment: BrokerEnvironment) -> Callable[[OrderIntent], str]:
        constructed.append(environment)
        return lambda _parameters: "broker-1"

    dependencies = _dependencies(
        plan,
        checks=CurrentStateChecks(
            portfolio_unchanged=lambda _plan, _fills: False,
            market_unchanged=lambda _plan, _fills: True,
            cash_unchanged=lambda _plan, _fills: True,
            open_orders_unchanged=lambda _plan, _fills: True,
            evidence_fresh=lambda _plan, _fills: True,
            tax_state_unchanged=lambda _plan, _fills: True,
            account_ready=lambda _plan, _fills: True,
            market_fresh=lambda _plan, _fills: True,
        ),
        factory=factory,
    )

    with pytest.raises(PreflightDeniedError):
        _ = execute_plan(plan.payload.plan_id, _policy(), dependencies)

    assert constructed == []


def test_execute_plan_does_not_construct_writer_with_unreconciled_claim() -> None:
    plan = _plan()
    constructed: list[BrokerEnvironment] = []
    claim = ExecutionClaim(
        plan_hash=plan.plan_hash,
        trade_identity="prior-leg",
        status="claimed",
        claimed_at=_NOW,
        updated_at=_NOW,
    )
    dependencies = _dependencies(
        plan,
        claims=_Claims(unresolved=(claim,)),
        factory=lambda environment: constructed.append(environment) or (lambda _parameters: "broker-1"),
    )

    with pytest.raises(RecoveryRequiredError):
        _ = execute_plan(plan.payload.plan_id, _policy(), dependencies)

    assert constructed == []


def test_execute_plan_revalidates_every_current_state_check_before_each_leg() -> None:
    plan = _plan(
        trades=(
            ProposedTrade(
                instrument="AAPL",
                asset_class=TradableAssetClass.US_EQUITY,
                side="buy",
                quantity=1,
                estimated_notional=200,
                tax_cost_known=True,
            ),
            ProposedTrade(
                instrument="MSFT",
                asset_class=TradableAssetClass.US_EQUITY,
                side="buy",
                quantity=1,
                estimated_notional=300,
                tax_cost_known=True,
            ),
        )
    )
    calls: list[str] = []

    def check(name: str) -> Callable[[PortfolioPlan, tuple[ConfirmedFill, ...]], bool]:
        def current(_plan: PortfolioPlan, _confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
            calls.append(name)
            return True

        return current

    checks = CurrentStateChecks(
        portfolio_unchanged=check("portfolio"),
        market_unchanged=check("market"),
        cash_unchanged=check("cash"),
        open_orders_unchanged=check("open_orders"),
        evidence_fresh=check("evidence"),
        tax_state_unchanged=check("tax"),
        account_ready=check("account"),
        market_fresh=check("market_fresh"),
    )

    _ = execute_plan(plan.payload.plan_id, _policy(), _dependencies(plan, checks=checks))

    assert calls == ["portfolio", "market", "cash", "open_orders", "evidence", "tax", "account", "market_fresh"] * 3


def test_execute_plan_persists_intent_and_claim_before_submission() -> None:
    plan = _plan()
    trace: list[str] = []

    class TraceClaims(_Claims):
        @override
        def claim(self, plan_hash: str, trade_identity: str, *, claimed_at: datetime) -> None:
            trace.append("claim")
            super().claim(plan_hash, trade_identity, claimed_at=claimed_at)

    class TraceJournal(_Journal):
        @override
        def append_event(self, event: ExecutionEvent) -> None:
            trace.append(f"event:{event.phase.value}")
            super().append_event(event)

    def place(_parameters: OrderIntent) -> str:
        trace.append("submit")
        return "broker-1"

    _ = execute_plan(
        plan.payload.plan_id,
        _policy(),
        _dependencies(plan, claims=TraceClaims(), journal=TraceJournal(), factory=lambda _environment: place),
    )

    assert trace[:4] == ["event:intent_recorded", "claim", "event:claimed", "submit"]


def test_execute_plan_submission_uncertainty_leaves_nonterminal_claim_for_recovery() -> None:
    plan = _plan()
    claims = _Claims()
    journal = _Journal()

    def uncertain(_parameters: OrderIntent) -> str:
        raise TimeoutError("broker result unknown")

    with pytest.raises(BrokerSubmissionError):
        _ = execute_plan(
            plan.payload.plan_id,
            _policy(),
            _dependencies(plan, claims=claims, journal=journal, factory=lambda _environment: uncertain),
        )

    assert (claims.trace, journal.events[-1].phase) == (
        [f"claim:{journal.events[0].trade_identity}"],
        ExecutionEventPhase.FAILED,
    )


def test_execute_plan_returns_only_after_terminal_fill_is_durable() -> None:
    plan = _plan()
    claims = _Claims()
    journal = _Journal()

    receipt = execute_plan(
        plan.payload.plan_id,
        _policy(),
        _dependencies(plan, claims=claims, journal=journal),
    )

    assert (
        receipt.completed_trade_identities,
        claims.trace[-1].split(":")[2],
        journal.events[-1].phase,
    ) == (receipt.submitted_trade_identities, "filled", ExecutionEventPhase.FILLED)


def test_execute_plan_journals_distinct_partial_and_terminal_fill_states() -> None:
    plan = _plan()
    journal = _Journal()
    observations = iter(
        (
            FillObservation(
                phase=ExecutionPhase.PARTIALLY_FILLED,
                status="partially_filled",
                filled_qty=0.5,
                filled_avg_price=100,
                realized_notional=50,
            ),
            FillObservation(
                phase=ExecutionPhase.FILLED,
                status="filled",
                filled_qty=1,
                filled_avg_price=101,
                realized_notional=101,
            ),
        )
    )

    _ = execute_plan(
        plan.payload.plan_id,
        _policy(),
        _dependencies(plan, journal=journal, observe_fill=lambda _identity: next(observations)),
    )

    assert [event.phase for event in journal.events[-2:]] == [
        ExecutionEventPhase.PARTIALLY_FILLED,
        ExecutionEventPhase.FILLED,
    ]


@pytest.mark.parametrize("observable", [False, True])
def test_execute_plan_halts_subsequent_legs_when_observation_cannot_confirm_fill(observable: bool) -> None:
    plan = _plan(
        trades=(
            ProposedTrade(
                instrument="AAPL",
                asset_class=TradableAssetClass.US_EQUITY,
                side="buy",
                quantity=1,
                estimated_notional=200,
                tax_cost_known=True,
            ),
            ProposedTrade(
                instrument="MSFT",
                asset_class=TradableAssetClass.US_EQUITY,
                side="buy",
                quantity=1,
                estimated_notional=300,
                tax_cost_known=True,
            ),
        )
    )
    submitted: list[str] = []
    journal = _Journal()
    monotonic_values = iter((0.0, 1.0, 2.0))

    def place(parameters: OrderIntent) -> str:
        submitted.append(parameters.client_order_id)
        return "broker-1"

    def observe(_identity: str) -> FillObservation:
        if not observable:
            raise BrokerObservationUnavailableError("broker read failed")
        return FillObservation(
            phase=ExecutionPhase.SUBMITTED,
            status="new",
            filled_qty=0,
            filled_avg_price=None,
            realized_notional=None,
        )

    with pytest.raises(RecoveryRequiredError):
        _ = execute_plan(
            plan.payload.plan_id,
            _policy(),
            _dependencies(
                plan,
                journal=journal,
                factory=lambda _environment: place,
                observe_fill=observe,
                monotonic=lambda: next(monotonic_values),
            ),
        )

    assert (len(submitted), journal.events[-1].phase) == (1, ExecutionEventPhase.RECOVERY_HALTED)
