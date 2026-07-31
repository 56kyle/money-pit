"""Tests for immutable portfolio-plan decisions and authorization."""

from datetime import datetime
from datetime import timedelta

import pytest

from money_pit.plans.errors import ExecutionDisabledError
from money_pit.plans.errors import PlanApprovalRequiredError
from money_pit.plans.errors import PlanExpiredError
from money_pit.plans.errors import PlanGateFailedError
from money_pit.plans.errors import PlanHashMismatchError
from money_pit.plans.errors import PlanStateDriftError
from money_pit.plans.lifecycle import AuthorizationBasis
from money_pit.plans.lifecycle import AuthorizationContext
from money_pit.plans.lifecycle import approve_plan
from money_pit.plans.lifecycle import authorize_plan
from money_pit.plans.lifecycle import recompute_plan_hash
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade


def test_recompute_plan_hash_matches_constructed_plan(
    portfolio_plan: PortfolioPlan,
) -> None:
    assert recompute_plan_hash(portfolio_plan.payload) == portfolio_plan.plan_hash


def test_approve_plan_with_tampered_hash_rejects_plan(
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    tampered: PortfolioPlan = PortfolioPlan.model_construct(
        payload=portfolio_plan.payload,
        plan_hash="0" * 64,
    )

    with pytest.raises(PlanHashMismatchError):
        approve_plan(tampered, decision_id="decision-1", decided_at=now, decided_by="operator")


def test_authorize_plan_with_exact_approval_grants_operator_authority(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
) -> None:
    approval = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
    )

    authorization = authorize_plan(
        portfolio_plan,
        execution_policy=approval_required_policy,
        context=authorization_context,
        decision=approval,
    )

    assert authorization.basis is AuthorizationBasis.OPERATOR_APPROVAL


def test_authorize_plan_without_approval_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
) -> None:
    with pytest.raises(PlanApprovalRequiredError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=authorization_context,
        )


def test_authorize_plan_at_expiry_rejects_plan(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
) -> None:
    approval = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
    )
    expired_context: AuthorizationContext = authorization_context.model_copy(
        update={"evaluated_at": portfolio_plan.payload.expires_at}
    )

    with pytest.raises(PlanExpiredError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=expired_context,
            decision=approval,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("portfolio_snapshot_id", "portfolio-2", id="portfolio"),
        pytest.param("market_snapshot_id", "market-2", id="market"),
        pytest.param("policy_version", "policy-2", id="policy"),
        pytest.param("evidence_gate_results", {"independent_support": False}, id="evidence"),
        pytest.param("constraint_results", {"position_limit": False}, id="constraint"),
    ],
)
def test_authorize_plan_with_state_drift_rejects_plan(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
    field: str,
    value: object,
) -> None:
    approval = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
    )
    drifted: AuthorizationContext = authorization_context.model_copy(update={field: value})

    with pytest.raises(PlanStateDriftError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=drifted,
            decision=approval,
        )


def test_authorize_plan_with_kill_switch_rejects_plan(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
) -> None:
    disabled: AuthorizationContext = authorization_context.model_copy(update={"execution_enabled": False})

    with pytest.raises(ExecutionDisabledError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=disabled,
        )


def test_authorize_plan_in_observe_mode_rejects_plan(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
) -> None:
    observe: ExecutionPolicy = approval_required_policy.model_copy(update={"execution_mode": ExecutionMode.OBSERVE})

    with pytest.raises(ExecutionDisabledError):
        authorize_plan(
            portfolio_plan,
            execution_policy=observe,
            context=authorization_context,
        )


def test_authorize_plan_autonomous_tax_unknown_sell_fails_closed(
    portfolio_plan_payload: PortfolioPlanPayload,
    autonomous_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
) -> None:
    tax_unknown_sell = ProposedTrade(
        instrument="AAPL",
        side="sell",
        quantity=1.0,
        estimated_notional=200.0,
        tax_cost_known=False,
    )
    payload: PortfolioPlanPayload = portfolio_plan_payload.model_copy(update={"proposed_trades": (tax_unknown_sell,)})
    plan: PortfolioPlan = PortfolioPlan.from_payload(payload)

    with pytest.raises(PlanGateFailedError):
        authorize_plan(
            plan,
            execution_policy=autonomous_policy,
            context=authorization_context,
        )


def test_authorize_plan_repeated_call_is_deterministic(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
) -> None:
    approval = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
    )

    first = authorize_plan(
        portfolio_plan,
        execution_policy=approval_required_policy,
        context=authorization_context,
        decision=approval,
    )
    second = authorize_plan(
        portfolio_plan,
        execution_policy=approval_required_policy,
        context=authorization_context,
        decision=approval,
    )

    assert second == first


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("maximum_order_notional", 300.0, id="order-notional"),
        pytest.param("maximum_daily_turnover", 0.05, id="daily-turnover"),
        pytest.param("allowed_asset_classes", ("crypto",), id="asset-class"),
    ],
)
def test_authorize_plan_with_uncovered_execution_policy_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
    field: str,
    value: object,
) -> None:
    approval = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
    )
    restrictive: ExecutionPolicy = approval_required_policy.model_copy(update={field: value})

    with pytest.raises(PlanGateFailedError):
        authorize_plan(
            portfolio_plan,
            execution_policy=restrictive,
            context=authorization_context,
            decision=approval,
        )


def test_authorize_plan_with_approval_before_plan_creation_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
) -> None:
    approval = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now - timedelta(seconds=1),
        decided_by="operator",
    )

    with pytest.raises(PlanApprovalRequiredError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=authorization_context,
            decision=approval,
        )


def test_authorize_plan_with_future_approval_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
) -> None:
    approval = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=authorization_context.evaluated_at + timedelta(seconds=1),
        decided_by="operator",
    )

    with pytest.raises(PlanApprovalRequiredError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=authorization_context,
            decision=approval,
        )
