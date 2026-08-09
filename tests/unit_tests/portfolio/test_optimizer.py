"""Tests for deterministic constrained portfolio optimization."""

import math

import numpy as np
import pytest

from money_pit.portfolio.errors import IncompletePortfolioPolicyError
from money_pit.portfolio.errors import InvalidOptimizationInputError
from money_pit.portfolio.errors import OptimizationResultError
from money_pit.portfolio.optimizer import ClarabelOptimizer
from money_pit.portfolio.optimizer import OptimizationInput
from money_pit.portfolio.optimizer import OptimizationResult
from money_pit.portfolio.optimizer import grandfathered_signed_bounds
from money_pit.portfolio.policy import PortfolioPolicy
from money_pit.schemas.instrument import InstrumentExposureClass


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
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
) -> None:
    assert all(
        weight <= optimization_input.maximum_weights[instrument] + portfolio_policy.feasibility_tolerance
        for instrument, weight in optimization_result.target_weights.items()
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
    tighter: OptimizationInput = optimization_input.model_copy(
        update={"maximum_weights": {**optimization_input.maximum_weights, "SPY": 0.5}}
    )
    constrained: OptimizationResult = optimizer.optimize(tighter, portfolio_policy)

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


def _overweight_input() -> OptimizationInput:
    return OptimizationInput(
        portfolio_snapshot_id="portfolio-overweight",
        market_snapshot_id="market-overweight",
        current_weights={"AAPL": 0.6, "VTI": 0.2},
        expected_returns={"AAPL": 0.1, "VTI": 0.08},
        covariance={"AAPL": {"AAPL": 0.1, "VTI": 0.02}, "VTI": {"AAPL": 0.02, "VTI": 0.05}},
        sectors={"AAPL": "technology", "VTI": "broad_market"},
        exposure_classes={
            "AAPL": InstrumentExposureClass.SINGLE_STOCK,
            "VTI": InstrumentExposureClass.BROAD_MARKET_EQUITY_ETF,
        },
        tax_cost_per_sold_weight={"AAPL": 0.0, "VTI": 0.0},
        tax_cost_known={"AAPL": True, "VTI": True},
        maximum_weights={"AAPL": 0.6, "VTI": 0.35},
        factor_loadings={"AAPL": {"growth": 1.0}, "VTI": {"growth": 0.5}},
        correlated_groups={"us_growth": frozenset({"AAPL", "VTI"})},
    )


def _overweight_policy() -> PortfolioPolicy:
    return PortfolioPolicy(
        policy_version="grandfathering-1",
        risk_aversion=0.35,
        turnover_penalty=0.01,
        tax_penalty=1.0,
        minimum_cash_weight=0.1,
        maximum_equity_exposure=0.9,
        maximum_single_stock_exposure=0.15,
        maximum_thematic_etf_exposure=0.4,
        maximum_turnover=0.3,
        minimum_trade_weight=0.0,
        maximum_position_change=0.1,
        maximum_sector_weights={"technology": 0.4, "broad_market": 0.4},
        maximum_factor_exposures={"growth": 0.5},
        maximum_correlated_group_weights={"us_growth": 0.45},
        accept_optimal_inaccurate=False,
        feasibility_tolerance=1e-6,
    )


@pytest.mark.parametrize(
    "target_weights",
    [pytest.param([0.6, 0.2], id="unchanged"), pytest.param([0.55, 0.2], id="reduced")],
)
def test__validate_result_grandfathers_unchanged_or_reduced_overweight_exposure(
    target_weights: list[float],
) -> None:
    optimization_input = _overweight_input()
    current = np.asarray([0.6, 0.2])

    ClarabelOptimizer._validate_result(  # pyright: ignore[reportPrivateUsage]  # Contract test pins private grandfathering rules.
        ("AAPL", "VTI"),
        np.asarray(target_weights),
        current,
        optimization_input,
        _overweight_policy(),
    )


def test__validate_result_rejects_increasing_an_overweight_position() -> None:
    optimization_input = _overweight_input().model_copy(update={"maximum_weights": {"AAPL": 0.6, "VTI": 0.35}})

    with pytest.raises(OptimizationResultError):
        ClarabelOptimizer._validate_result(  # pyright: ignore[reportPrivateUsage]  # Contract test pins private grandfathering rules.
            ("AAPL", "VTI"),
            np.asarray([0.61, 0.2]),
            np.asarray([0.6, 0.2]),
            optimization_input,
            _overweight_policy(),
        )


def test_optimize_does_not_force_one_step_liquidation_of_an_overweight_position() -> None:
    result = ClarabelOptimizer().optimize(_overweight_input(), _overweight_policy())

    assert result.target_weights["AAPL"] >= 0.5 - _overweight_policy().feasibility_tolerance


@pytest.mark.parametrize(
    ("policy_update", "target_weights"),
    [
        pytest.param(
            {
                "maximum_sector_weights": {"technology": 0.9, "broad_market": 0.9},
                "maximum_factor_exposures": {"growth": 2.0},
                "maximum_correlated_group_weights": {"us_growth": 0.9},
            },
            [0.61, 0.19],
            id="exposure-class",
        ),
        pytest.param(
            {
                "maximum_single_stock_exposure": 0.9,
                "maximum_factor_exposures": {"growth": 2.0},
                "maximum_correlated_group_weights": {"us_growth": 0.9},
            },
            [0.61, 0.19],
            id="sector",
        ),
        pytest.param(
            {
                "maximum_single_stock_exposure": 0.9,
                "maximum_sector_weights": {"technology": 0.9, "broad_market": 0.9},
                "maximum_correlated_group_weights": {"us_growth": 0.9},
            },
            [0.61, 0.19],
            id="factor",
        ),
        pytest.param(
            {
                "maximum_equity_exposure": 0.95,
                "maximum_single_stock_exposure": 0.9,
                "maximum_sector_weights": {"technology": 0.9, "broad_market": 0.9},
                "maximum_factor_exposures": {"growth": 2.0},
            },
            [0.61, 0.2],
            id="correlation",
        ),
    ],
)
def test__validate_exposure_result_rejects_increasing_grandfathered_exposure(
    policy_update: dict[str, object],
    target_weights: list[float],
) -> None:
    policy = _overweight_policy().model_copy(update=policy_update)

    with pytest.raises(OptimizationResultError):
        ClarabelOptimizer._validate_exposure_result(  # pyright: ignore[reportPrivateUsage]  # Contract test pins private grandfathering rules.
            ("AAPL", "VTI"),
            np.asarray(target_weights),
            _overweight_input(),
            policy,
        )


@pytest.mark.parametrize(
    ("current_exposure", "expected_bounds"),
    [
        pytest.param(0.0, (-0.5, 0.5), id="within-limit"),
        pytest.param(0.7, (-0.5, 0.7), id="positive-grandfathering"),
        pytest.param(-0.7, (-0.7, 0.5), id="negative-grandfathering"),
    ],
)
def test_grandfathered_signed_bounds_preserves_only_the_existing_breach_side(
    current_exposure: float,
    expected_bounds: tuple[float, float],
) -> None:
    assert grandfathered_signed_bounds(0.5, current_exposure) == expected_bounds


def test__validate_exposure_result_rejects_crossing_into_the_opposite_factor_breach() -> None:
    optimization_input = OptimizationInput(
        portfolio_snapshot_id="portfolio-factor",
        market_snapshot_id="market-factor",
        current_weights={"AAPL": 0.7, "HEDGE": 0.0},
        expected_returns={"AAPL": 0.1, "HEDGE": 0.1},
        covariance={"AAPL": {"AAPL": 0.1, "HEDGE": 0.0}, "HEDGE": {"AAPL": 0.0, "HEDGE": 0.1}},
        sectors={"AAPL": "technology", "HEDGE": "broad_market"},
        exposure_classes={
            "AAPL": InstrumentExposureClass.SINGLE_STOCK,
            "HEDGE": InstrumentExposureClass.BROAD_MARKET_EQUITY_ETF,
        },
        tax_cost_per_sold_weight={"AAPL": 0.0, "HEDGE": 0.0},
        tax_cost_known={"AAPL": True, "HEDGE": True},
        factor_loadings={"AAPL": {"signed": 1.0}, "HEDGE": {"signed": -1.0}},
    )
    policy = _overweight_policy().model_copy(
        update={
            "maximum_single_stock_exposure": 0.9,
            "maximum_sector_weights": {"technology": 0.9, "broad_market": 0.9},
            "maximum_factor_exposures": {"signed": 0.5},
            "maximum_correlated_group_weights": {},
        }
    )

    with pytest.raises(OptimizationResultError):
        ClarabelOptimizer._validate_exposure_result(  # pyright: ignore[reportPrivateUsage]  # Contract test pins signed-factor grandfathering.
            ("AAPL", "HEDGE"),
            np.asarray([0.0, 0.6]),
            optimization_input,
            policy,
        )
