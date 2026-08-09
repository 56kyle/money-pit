"""Tests for deterministic constrained portfolio optimization."""

import math

import pytest

from money_pit.portfolio.errors import IncompletePortfolioPolicyError
from money_pit.portfolio.errors import InvalidOptimizationInputError
from money_pit.portfolio.optimizer import ClarabelOptimizer
from money_pit.portfolio.optimizer import OptimizationInput
from money_pit.portfolio.optimizer import OptimizationResult
from money_pit.portfolio.policy import PortfolioPolicy


@pytest.fixture
def optimization_result(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
) -> OptimizationResult:
    return ClarabelOptimizer().optimize(optimization_input, portfolio_policy)


def test_optimize_conserves_cash(optimization_result: OptimizationResult) -> None:
    invested: float = sum(optimization_result.target_weights.values())
    assert math.isclose(invested + optimization_result.cash_weight, 1.0, abs_tol=1e-9)


def test_optimize_respects_position_bounds(
    optimization_result: OptimizationResult,
    portfolio_policy: PortfolioPolicy,
) -> None:
    assert max(optimization_result.target_weights.values()) <= (
        portfolio_policy.maximum_position_weight + portfolio_policy.feasibility_tolerance
    )


def test_optimize_respects_turnover(
    optimization_result: OptimizationResult,
    portfolio_policy: PortfolioPolicy,
) -> None:
    assert optimization_result.turnover <= portfolio_policy.maximum_turnover + portfolio_policy.feasibility_tolerance


def test_optimize_turnover_includes_cash_change(
    optimization_result: OptimizationResult,
    optimization_input: OptimizationInput,
) -> None:
    risky_turnover: float = sum(
        abs(optimization_result.target_weights[instrument] - weight)
        for instrument, weight in optimization_input.current_weights.items()
    )
    current_cash: float = 1.0 - sum(optimization_input.current_weights.values())
    assert math.isclose(
        optimization_result.turnover,
        0.5 * (risky_turnover + abs(optimization_result.cash_weight - current_cash)),
    )


def test_optimize_respects_sector_limits(
    optimization_result: OptimizationResult,
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
) -> None:
    technology_weight: float = sum(
        weight
        for instrument, weight in optimization_result.target_weights.items()
        if optimization_input.sectors[instrument] == "technology"
    )
    assert technology_weight <= (
        portfolio_policy.maximum_sector_weights["technology"] + portfolio_policy.feasibility_tolerance
    )


def test_optimize_is_deterministic(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
) -> None:
    optimizer = ClarabelOptimizer()
    first: OptimizationResult = optimizer.optimize(optimization_input, portfolio_policy)
    second: OptimizationResult = optimizer.optimize(optimization_input, portfolio_policy)

    assert second == first


def test_optimize_with_tighter_position_bound_does_not_improve_objective(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
) -> None:
    optimizer = ClarabelOptimizer()
    baseline: OptimizationResult = optimizer.optimize(optimization_input, portfolio_policy)
    tighter: PortfolioPolicy = portfolio_policy.model_copy(update={"maximum_position_weight": 0.5})
    constrained: OptimizationResult = optimizer.optimize(optimization_input, tighter)

    assert constrained.objective_value <= baseline.objective_value + portfolio_policy.feasibility_tolerance


def test_optimize_with_incomplete_sector_policy_fails_closed(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
) -> None:
    incomplete: PortfolioPolicy = portfolio_policy.model_copy(update={"maximum_sector_weights": {"technology": 0.3}})

    with pytest.raises(IncompletePortfolioPolicyError):
        _ = ClarabelOptimizer().optimize(optimization_input, incomplete)


def test_optimize_with_non_finite_expected_return_fails_closed(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
) -> None:
    invalid: OptimizationInput = optimization_input.model_copy(
        update={"expected_returns": {**optimization_input.expected_returns, "AAPL": float("nan")}}
    )

    with pytest.raises(InvalidOptimizationInputError):
        _ = ClarabelOptimizer().optimize(invalid, portfolio_policy)
