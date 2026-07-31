"""Tests for stable plan-aware order construction."""

import pytest

from money_pit.execution_control.orders import execution_parameters_for_trade
from money_pit.execution_control.orders import plan_client_order_id
from money_pit.schemas.portfolio_plan import PortfolioPlan


def test_plan_client_order_id_is_stable(portfolio_plan: PortfolioPlan) -> None:
    first: str = plan_client_order_id(portfolio_plan, 0, "BRK.B")
    second: str = plan_client_order_id(portfolio_plan, 0, "BRK.B")

    assert second == first


def test_plan_client_order_id_distinguishes_trade_legs(portfolio_plan: PortfolioPlan) -> None:
    assert plan_client_order_id(portfolio_plan, 0, "AAPL") != plan_client_order_id(
        portfolio_plan,
        1,
        "AAPL",
    )


def test_execution_parameters_for_trade_binds_client_order_id(
    portfolio_plan: PortfolioPlan,
) -> None:
    trade = portfolio_plan.payload.proposed_trades[0]

    parameters = execution_parameters_for_trade(portfolio_plan, 0, trade)

    assert parameters.client_order_id == plan_client_order_id(portfolio_plan, 0, trade.instrument)


def test_execution_parameters_for_trade_rejects_invalid_order_side(
    portfolio_plan: PortfolioPlan,
) -> None:
    trade = portfolio_plan.payload.proposed_trades[0].model_copy(update={"side": "hold"})

    with pytest.raises(ValueError, match="Unsupported order side"):
        execution_parameters_for_trade(portfolio_plan, 0, trade)
