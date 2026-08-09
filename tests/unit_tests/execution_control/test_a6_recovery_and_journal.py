"""Tests for A6 recovery, enablement, and durable event journaling."""

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime

import pytest

from money_pit.alpaca_orders import OrderNotYetVisibleError
from money_pit.execution_control.fills import ExecutionPhase
from money_pit.execution_control.fills import FillObservation
from money_pit.execution_control.models import ExecutionClaim
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.models import ExecutionEvent
from money_pit.execution_control.models import ExecutionEventPhase
from money_pit.execution_control.recovery import reconcile_nonterminal_claims
from money_pit.execution_control.repository import ExecutionClaimStatus
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.execution_control.service import enable_execution
from money_pit.plans.lifecycle import approve_plan
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.storage.database import Database


_NOW = datetime(2026, 8, 9, 14, 0, tzinfo=UTC)
_PLAN_HASH = "3" * 64
_CONFIRMATION_REQUIRED = "explicit confirmation"


@dataclass
class _Claims:
    claims: tuple[ExecutionClaim, ...]
    updates: list[tuple[str, str]] = field(default_factory=list)

    def claim(self, plan_hash: str, trade_identity: str, *, claimed_at: datetime) -> None:
        del plan_hash, trade_identity, claimed_at
        raise AssertionError("recovery must not create claims")

    def update(
        self,
        plan_hash: str,
        trade_identity: str,
        *,
        status: str,
        updated_at: datetime,
        broker_order_id: str | None,
    ) -> None:
        del plan_hash, updated_at, broker_order_id
        self.updates.append((trade_identity, status))

    def nonterminal_claims(self) -> tuple[ExecutionClaim, ...]:
        return self.claims


@dataclass
class _Journal:
    events: list[ExecutionEvent]

    def append_event(self, event: ExecutionEvent) -> None:
        self.events.append(event)

    def events_for_plan(self, plan_hash: str) -> tuple[ExecutionEvent, ...]:
        return tuple(event for event in self.events if event.plan_hash == plan_hash)


def _claim(identity: str) -> ExecutionClaim:
    return ExecutionClaim(
        plan_hash=_PLAN_HASH,
        trade_identity=identity,
        status="submitted",
        claimed_at=_NOW,
        updated_at=_NOW,
        broker_order_id=f"broker-{identity}",
    )


def _intent(identity: str) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=f"intent-{identity}",
        plan_id="plan-1",
        plan_hash=_PLAN_HASH,
        trade_identity=identity,
        client_order_id=identity,
        phase=ExecutionEventPhase.INTENT_RECORDED,
        occurred_at=_NOW,
    )


def test_reconcile_nonterminal_claims_resolves_terminal_fill() -> None:
    claims = _Claims((_claim("leg-1"),))
    journal = _Journal([_intent("leg-1")])
    filled = FillObservation(
        phase=ExecutionPhase.FILLED,
        status="filled",
        filled_qty=1,
        filled_avg_price=100,
        realized_notional=100,
    )

    result = reconcile_nonterminal_claims(
        claims,
        journal,
        lambda _identity: filled,
        clock=lambda: _NOW,
        event_id_factory=lambda: "recovery-1",
    )

    assert (claims.updates, result.reconciled_trade_identities, journal.events[-1].phase) == (
        [("leg-1", "filled")],
        ("leg-1",),
        ExecutionEventPhase.FILLED,
    )


@pytest.mark.parametrize("observable", [False, True])
def test_reconcile_nonterminal_claims_halts_on_unobservable_or_open_claim(observable: bool) -> None:
    claims = _Claims((_claim("leg-1"),))
    journal = _Journal([_intent("leg-1")])

    def observe(_identity: str) -> FillObservation:
        if not observable:
            raise OrderNotYetVisibleError("not indexed")
        return FillObservation(
            phase=ExecutionPhase.SUBMITTED,
            status="new",
            filled_qty=0,
            filled_avg_price=None,
            realized_notional=None,
        )

    result = reconcile_nonterminal_claims(
        claims,
        journal,
        observe,
        clock=lambda: _NOW,
        event_id_factory=lambda: "recovery-1",
    )

    assert (result.allows_new_execution, result.unresolved_trade_identities, journal.events[-1].phase) == (
        False,
        ("leg-1",),
        ExecutionEventPhase.RECOVERY_HALTED,
    )


@dataclass
class _KillSwitch:
    enabled_states: list[ExecutionControlState] = field(default_factory=list)

    def get_control_state(self) -> ExecutionControlState:
        raise AssertionError("enablement does not need a prior state read")

    def disable(self, state: ExecutionControlState) -> None:
        del state
        raise AssertionError("enablement must not disable execution")

    def enable(self, state: ExecutionControlState) -> None:
        self.enabled_states.append(state)


def test_enable_execution_requires_confirmation_before_persistence() -> None:
    kill_switch = _KillSwitch()

    with pytest.raises(ValueError, match=_CONFIRMATION_REQUIRED):
        _ = enable_execution(
            kill_switch,
            changed_at=_NOW,
            actor="operator",
            reason="maintenance complete",
            policy_version="policy-1",
            confirmed=False,
        )

    assert kill_switch.enabled_states == []


def test_enable_execution_persists_confirmed_policy_actor_and_reason() -> None:
    kill_switch = _KillSwitch()

    state = enable_execution(
        kill_switch,
        changed_at=_NOW,
        actor="operator",
        reason="maintenance complete",
        policy_version="policy-1",
        confirmed=True,
    )

    assert (kill_switch.enabled_states, state.policy_version, state.actor, state.reason) == (
        [state],
        "policy-1",
        "operator",
        "maintenance complete",
    )


def test_append_event_round_trips_when_baseline_exposes_journal(database: Database) -> None:
    repository = SqliteExecutionAuthorityRepository(database)
    event = _intent("leg-1")

    repository.append_event(event)

    assert repository.events_for_plan(_PLAN_HASH) == (event,)


def test_baseline_seeds_execution_disabled_with_explicit_unconfigured_policy(
    execution_repository: SqliteExecutionAuthorityRepository,
) -> None:
    state = execution_repository.get_control_state()

    assert (state.disabled, state.policy_version) == (True, "unconfigured")


def test_enable_execution_round_trips_through_baseline_repository(
    execution_repository: SqliteExecutionAuthorityRepository,
) -> None:
    expected = enable_execution(
        execution_repository,
        changed_at=_NOW,
        actor="operator",
        reason="approved execution policy installed",
        policy_version="policy-1",
        confirmed=True,
    )

    assert execution_repository.get_control_state() == expected


def test_claim_update_to_terminal_removes_nonterminal_baseline_record(
    execution_repository: SqliteExecutionAuthorityRepository,
) -> None:
    execution_repository.claim(_PLAN_HASH, "leg-1", claimed_at=_NOW)
    claimed = execution_repository.nonterminal_claims()

    execution_repository.update(
        _PLAN_HASH,
        "leg-1",
        status=ExecutionClaimStatus.FILLED,
        updated_at=_NOW,
        broker_order_id="broker-1",
    )

    assert (tuple(claim.status for claim in claimed), execution_repository.nonterminal_claims()) == (
        (ExecutionClaimStatus.CLAIMED,),
        (),
    )


def test_approval_decision_round_trips_for_repository_persisted_plan(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
) -> None:
    policy = ExecutionPolicy(
        policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        maximum_order_notional=1_000,
        maximum_daily_turnover=0.2,
    )
    approval = approve_plan(
        persisted_portfolio_plan,
        decision_id="approval-roundtrip",
        decided_at=_NOW,
        decided_by="operator",
        execution_config_hash="e" * 64,
        execution_policy=policy,
        account_id=persisted_portfolio_plan.payload.account_id,
        committed_turnover_at_approval=0.0,
    )

    execution_repository.append(approval)

    assert execution_repository.latest_for(persisted_portfolio_plan.payload.plan_id) == approval
