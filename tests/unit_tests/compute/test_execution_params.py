"""Tests for build_execution_params — positive-path coverage."""
import pytest

from money_pit.compute.execution_params import build_execution_params
from money_pit.schemas.enums import ActionType


def test_build_execution_params_buy_side() -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="NVDA",
        action_type=ActionType.BUY,
        dollar_amount=1500.0,
    )

    assert result.side == "buy"
    assert result.symbol == "NVDA"
    assert result.notional == pytest.approx(1500.0)
    assert result.client_order_id == "2026-01-01_00-00-00:A001"
    assert result.type == "market"
    assert result.time_in_force == "day"


def test_build_execution_params_sell_side() -> None:
    result = build_execution_params(
        step_id="A001",
        slug="2026-01-01_00-00-00",
        symbol="MSFT",
        action_type=ActionType.SELL,
        dollar_amount=750.0,
    )

    assert result.side == "sell"
    assert result.symbol == "MSFT"
