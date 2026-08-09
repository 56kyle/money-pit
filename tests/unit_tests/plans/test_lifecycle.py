from datetime import datetime

import pytest

from money_pit.plans.errors import PlanHashMismatchError
from money_pit.plans.lifecycle import approve_plan
from money_pit.plans.lifecycle import recompute_plan_hash
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan


def test_recompute_plan_hash_matches_constructed_plan(portfolio_plan: PortfolioPlan) -> None:
    assert recompute_plan_hash(portfolio_plan.payload) == portfolio_plan.plan_hash


def test_approve_plan_binds_the_exact_execution_and_account_authority(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    now: datetime,
) -> None:
    approval = approve_plan(
        portfolio_plan,
        decision_id="decision-1",
        decided_at=now,
        decided_by="operator",
        execution_config_hash="e" * 64,
        execution_policy=approval_required_policy,
        account_id=portfolio_plan.payload.account_id,
        committed_turnover_at_approval=0.15,
    )

    assert (
        approval.execution_config_hash,
        approval.execution_policy_hash,
        approval.execution_policy_version,
        approval.broker_environment,
        approval.account_id,
        approval.committed_turnover_at_approval,
    ) == (
        "e" * 64,
        approval_required_policy.fingerprint(),
        approval_required_policy.policy_version,
        approval_required_policy.broker_environment,
        portfolio_plan.payload.account_id,
        0.15,
    )


def test_approve_plan_rejects_a_tampered_plan(
    portfolio_plan: PortfolioPlan,
    approval_required_policy: ExecutionPolicy,
    now: datetime,
) -> None:
    tampered = PortfolioPlan.model_construct(payload=portfolio_plan.payload, plan_hash="0" * 64)

    with pytest.raises(PlanHashMismatchError):
        _ = approve_plan(
            tampered,
            decision_id="decision-1",
            decided_at=now,
            decided_by="operator",
            execution_config_hash="e" * 64,
            execution_policy=approval_required_policy,
            account_id=portfolio_plan.payload.account_id,
            committed_turnover_at_approval=0.0,
        )
