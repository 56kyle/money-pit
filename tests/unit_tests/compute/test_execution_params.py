"""Tests for build_execution_params — guards schema-pinning gate."""
import pytest

from money_pit.compute.execution_params import build_execution_params
from money_pit.schemas.enums import ActionType


def test_build_execution_params_raises_when_schema_missing() -> None:
    with pytest.raises(FileNotFoundError, match="alpaca_order_schema"):
        build_execution_params(
            step_id="A001",
            slug="2026-01-01_00-00-00",
            symbol="AAPL",
            action_type=ActionType.BUY,
            dollar_amount=1000.0,
        )
