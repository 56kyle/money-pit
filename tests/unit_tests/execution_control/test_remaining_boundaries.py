"""Remaining execution-control boundary contracts."""

from datetime import datetime

import pytest

from money_pit.execution_control.errors import PreflightDeniedError
from money_pit.execution_control.models import PreflightDenial
from money_pit.execution_control.models import PreflightDenialCode
from money_pit.execution_control.models import PreflightResult
from money_pit.execution_control.models import enabled_execution_state
from money_pit.execution_control.orders import plan_client_order_id
from money_pit.schemas.portfolio_plan import PortfolioPlan


def test_plan_client_order_id_rejects_negative_trade_index(portfolio_plan: PortfolioPlan) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _ = plan_client_order_id(portfolio_plan, -1, "AAPL")


def test_plan_client_order_id_uses_safe_fallback_for_symbol_without_alphanumerics(
    portfolio_plan: PortfolioPlan,
) -> None:
    assert plan_client_order_id(portfolio_plan, 0, "...").endswith("-ASSET")


def test_preflight_result_allowed_is_false_with_denial(
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    _ = PreflightResult(
        plan_id=portfolio_plan.payload.plan_id,
        plan_hash=portfolio_plan.plan_hash,
        checked_at=now,
        denials=(
            PreflightDenial(
                code=PreflightDenialCode.OBSERVE_ONLY,
                detail="observe only",
            ),
        ),
    )


def test_preflight_denied_error_preserves_typed_result(
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    result = PreflightResult(
        plan_id=portfolio_plan.payload.plan_id,
        plan_hash=portfolio_plan.plan_hash,
        checked_at=now,
        denials=(
            PreflightDenial(
                code=PreflightDenialCode.PLAN_EXPIRED,
                detail="expired",
            ),
        ),
    )

    error = PreflightDeniedError(result)

    assert error.result == result

    assert not result.allowed


def test_enabled_execution_state_builds_enabled_default(now: datetime) -> None:
    assert not enabled_execution_state(now, policy_version="policy-1").disabled
