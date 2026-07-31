"""Additional fail-closed authorization contract tests."""

from datetime import datetime
from datetime import timedelta

import pytest

from money_pit.plans.errors import PlanApprovalRequiredError
from money_pit.plans.errors import PlanGateFailedError
from money_pit.plans.errors import PlanHashMismatchError
from money_pit.plans.errors import PlanRejectedError
from money_pit.plans.errors import PlanStateDriftError
from money_pit.plans.lifecycle import AuthorizationBasis
from money_pit.plans.lifecycle import AuthorizationContext
from money_pit.plans.lifecycle import approve_plan
from money_pit.plans.lifecycle import authorize_plan
from money_pit.plans.lifecycle import reject_plan
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import ProposedTrade


def _approval(portfolio_plan: PortfolioPlan, now: datetime):
    return approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
    )


def test_reject_plan_with_tampered_hash_rejects_plan(
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    tampered = PortfolioPlan.model_construct(payload=portfolio_plan.payload, plan_hash="0" * 64)

    with pytest.raises(PlanHashMismatchError):
        reject_plan(
            tampered,
            decision_id="decision-1",
            decided_at=now,
            decided_by="operator",
            reason="risk changed",
        )


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"evidence_gate_results": {}}, id="missing-evidence"),
        pytest.param({"evidence_gate_results": {"support": False}}, id="failed-evidence"),
        pytest.param({"constraint_results": {}}, id="missing-constraints"),
        pytest.param({"constraint_results": {"position": False}}, id="failed-constraint"),
    ],
)
def test_authorize_plan_with_non_passing_gate_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
    update: dict[str, object],
) -> None:
    payload = portfolio_plan.payload.model_copy(update=update)
    plan = PortfolioPlan.from_payload(payload)
    context = authorization_context.model_copy(update=update)

    with pytest.raises(PlanGateFailedError):
        authorize_plan(
            plan,
            execution_policy=approval_required_policy,
            context=context,
            decision=_approval(plan, now),
        )


def test_authorize_plan_with_execution_policy_version_drift_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
) -> None:
    drifted = approval_required_policy.model_copy(update={"policy_version": "policy-2"})

    with pytest.raises(PlanStateDriftError):
        authorize_plan(
            portfolio_plan,
            execution_policy=drifted,
            context=authorization_context,
        )


def test_authorize_plan_with_rejection_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
) -> None:
    rejection = reject_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
        reason="risk changed",
    )

    with pytest.raises(PlanRejectedError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=authorization_context,
            decision=rejection,
        )


@pytest.mark.parametrize("field", ["plan_id", "plan_hash"])
def test_authorize_plan_with_mismatched_approval_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
    field: str,
) -> None:
    value = "different-plan" if field == "plan_id" else "0" * 64
    approval = _approval(portfolio_plan, now).model_copy(update={field: value})

    with pytest.raises(PlanApprovalRequiredError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=authorization_context,
            decision=approval,
        )


def test_authorize_plan_with_approval_after_expiry_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
) -> None:
    approval = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=portfolio_plan.payload.expires_at + timedelta(seconds=1),
        decided_by="operator",
    )

    with pytest.raises(PlanApprovalRequiredError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=authorization_context,
            decision=approval,
        )


def test_authorize_plan_with_missing_asset_class_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
) -> None:
    context = authorization_context.model_copy(update={"asset_classes": {}})

    with pytest.raises(PlanGateFailedError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=context,
            decision=_approval(portfolio_plan, now),
        )


def test_authorize_plan_autonomous_eligible_plan_grants_policy_authority(
    portfolio_plan: PortfolioPlan,
    autonomous_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
) -> None:
    authorization = authorize_plan(
        portfolio_plan,
        execution_policy=autonomous_policy,
        context=authorization_context,
    )

    assert authorization.basis is AuthorizationBasis.AUTONOMOUS_POLICY


def test_authorize_plan_autonomous_rejection_fails_closed(
    portfolio_plan: PortfolioPlan,
    autonomous_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
) -> None:
    rejection = reject_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
        reason="operator veto",
    )

    with pytest.raises(PlanRejectedError):
        authorize_plan(
            portfolio_plan,
            execution_policy=autonomous_policy,
            context=authorization_context,
            decision=rejection,
        )


def test_authorize_plan_with_committed_turnover_fails_closed(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
    now: datetime,
) -> None:
    context = authorization_context.model_copy(update={"committed_turnover": 0.15})

    with pytest.raises(PlanGateFailedError):
        authorize_plan(
            portfolio_plan,
            execution_policy=approval_required_policy,
            context=context,
            decision=_approval(portfolio_plan, now),
        )


def test_authorize_plan_with_tax_known_sell_remains_eligible_for_autonomy(
    portfolio_plan: PortfolioPlan,
    autonomous_policy: ExecutionPolicy,
    authorization_context: AuthorizationContext,
) -> None:
    trade = ProposedTrade(
        instrument="AAPL",
        side="sell",
        quantity=1.0,
        estimated_notional=200.0,
        tax_cost_known=True,
    )
    plan = PortfolioPlan.from_payload(portfolio_plan.payload.model_copy(update={"proposed_trades": (trade,)}))

    authorization = authorize_plan(
        plan,
        execution_policy=autonomous_policy,
        context=authorization_context,
    )

    assert authorization.basis is AuthorizationBasis.AUTONOMOUS_POLICY
