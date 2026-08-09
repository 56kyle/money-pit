"""Tests for portfolio plan risk and return delta semantics."""

import math

from money_pit.portfolio.optimizer import OptimizationInput
from money_pit.portfolio.planning import _expected_return_delta  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.planning import _expected_risk_delta  # pyright: ignore[reportPrivateUsage]


def test__expected_return_delta_subtracts_current_portfolio_return(
    optimization_input: OptimizationInput,
) -> None:
    targets = dict(optimization_input.current_weights)
    targets["AAPL"] += 0.1

    delta = _expected_return_delta(optimization_input, targets)

    assert math.isclose(delta, 0.012)


def test__expected_risk_delta_subtracts_current_portfolio_variance(
    optimization_input: OptimizationInput,
) -> None:
    targets = dict(optimization_input.current_weights)
    targets["AAPL"] += 0.1
    current = optimization_input.current_weights
    covariance = optimization_input.covariance
    expected = sum(
        (targets[left] * targets[right] - current[left] * current[right]) * covariance[left][right]
        for left in targets
        for right in targets
    )

    delta = _expected_risk_delta(optimization_input, targets)

    assert math.isclose(delta, expected)
