"""Failure-contract tests for deterministic optimizer validation."""

import importlib.metadata
from dataclasses import dataclass
from typing import cast

import cvxpy as cp
import numpy as np
import pytest
from cvxpy.error import SolverError
from pydantic import ValidationError
from pytest import MonkeyPatch

from money_pit.portfolio.errors import IncompletePortfolioPolicyError
from money_pit.portfolio.errors import InvalidOptimizationInputError
from money_pit.portfolio.errors import OptimizationFailedError
from money_pit.portfolio.errors import OptimizationResultError
from money_pit.portfolio.errors import OptimizerUnavailableError
from money_pit.portfolio.optimizer import ClarabelOptimizer
from money_pit.portfolio.optimizer import OptimizationInput
from money_pit.portfolio.policy import PortfolioPolicy


@dataclass
class _Problem:
    status: str


@dataclass
class _Weights:
    value: object | None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="bool"),
        pytest.param("1.0", id="non-real"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinite"),
    ],
)
def test__finite_solver_objective_rejects_invalid_value(value: object) -> None:
    with pytest.raises(OptimizationFailedError, match="no finite objective"):
        _ = ClarabelOptimizer._finite_solver_objective(value)  # pyright: ignore[reportPrivateUsage]  # Contract test pins private solver validation.


@pytest.mark.parametrize(
    ("value", "expected_count"),
    [
        pytest.param(object(), 1, id="conversion"),
        pytest.param([0.5], 2, id="shape"),
        pytest.param([0.5, float("nan")], 2, id="non-finite"),
    ],
)
def test__validated_solver_weights_rejects_malformed_value(
    value: object,
    expected_count: int,
) -> None:
    weights = cast("cp.Variable", cast("object", _Weights(value)))
    with pytest.raises(OptimizationFailedError, match="malformed weights"):
        _ = ClarabelOptimizer._validated_solver_weights(weights, expected_count)  # pyright: ignore[reportPrivateUsage]  # Contract test pins private solver validation.


@pytest.mark.parametrize(
    ("update", "message"),
    [
        pytest.param({"expected_returns": {}}, "must not be empty", id="empty"),
        pytest.param({"current_weights": {"AAPL": 0.1}}, "current_weights", id="current-map"),
        pytest.param({"covariance": {"AAPL": {"AAPL": 0.1}}}, "covariance rows", id="covariance-map"),
        pytest.param({"sectors": {"AAPL": "technology"}}, "sectors", id="sector-map"),
        pytest.param(
            {"tax_cost_per_sold_weight": {"AAPL": 0.0}},
            "tax_cost_per_sold_weight",
            id="tax-map",
        ),
        pytest.param({"satellite_instruments": {"UNKNOWN"}}, "unknown instrument", id="satellite"),
        pytest.param(
            {
                "covariance": {
                    "AAPL": {"AAPL": 0.09},
                    "SPY": {"AAPL": 0.018, "SPY": 0.04, "XOM": 0.012},
                    "XOM": {"AAPL": 0.009, "SPY": 0.012, "XOM": 0.06},
                }
            },
            "is incomplete",
            id="covariance-row",
        ),
        pytest.param(
            {"current_weights": {"AAPL": -0.1, "SPY": 0.5, "XOM": 0.1}},
            "between zero and one",
            id="negative-current",
        ),
        pytest.param(
            {"current_weights": {"AAPL": 0.5, "SPY": 0.5, "XOM": 0.1}},
            "exceed the portfolio",
            id="overinvested",
        ),
        pytest.param(
            {"tax_cost_per_sold_weight": {"AAPL": -0.1, "SPY": 0.0, "XOM": 0.0}},
            "cannot be negative",
            id="negative-tax",
        ),
    ],
)
def test_validate_instruments_rejects_inconsistent_inputs(
    optimization_input: OptimizationInput,
    update: dict[str, object],
    message: str,
) -> None:
    values = {**optimization_input.model_dump(), **update}

    with pytest.raises(ValidationError, match=message):
        _ = OptimizationInput.model_validate(values)


@pytest.mark.parametrize(
    ("covariance", "message"),
    [
        pytest.param(np.array([[1.0, np.nan], [np.nan, 1.0]]), "non-finite", id="non-finite"),
        pytest.param(np.array([[1.0, 0.2], [0.1, 1.0]]), "symmetric", id="asymmetric"),
        pytest.param(np.array([[1.0, 2.0], [2.0, 1.0]]), "positive semidefinite", id="indefinite"),
    ],
)
def test__validate_covariance_rejects_invalid_matrix(
    covariance: np.ndarray,
    message: str,
) -> None:
    with pytest.raises(InvalidOptimizationInputError, match=message):
        ClarabelOptimizer._validate_covariance(covariance)  # pyright: ignore[reportPrivateUsage]  # Contract test pins private covariance validation.


@pytest.mark.parametrize(
    ("policy_update", "input_update", "message"),
    [
        pytest.param({"minimum_core_weights": {"QQQ": 0.2}}, {}, "unknown instruments", id="core"),
        pytest.param(
            {"maximum_sector_weights": {"technology": 0.3, "broad_market": 0.6}},
            {},
            "sector policy is incomplete",
            id="sector",
        ),
        pytest.param({}, {"satellite_instruments": {"XOM"}}, "classified as core or satellite", id="class"),
    ],
)
def test__validate_policy_coverage_rejects_incomplete_policy(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
    policy_update: dict[str, object],
    input_update: dict[str, object],
    message: str,
) -> None:
    policy = portfolio_policy.model_copy(update=policy_update)
    inputs = optimization_input.model_copy(update=input_update)

    with pytest.raises(IncompletePortfolioPolicyError, match=message):
        ClarabelOptimizer._validate_policy_coverage(inputs, policy)  # pyright: ignore[reportPrivateUsage]  # Contract test pins private policy validation.


def test__require_acceptable_solution_accepts_configured_inaccurate_status(
    portfolio_policy: PortfolioPolicy,
) -> None:
    policy = portfolio_policy.model_copy(update={"accept_optimal_inaccurate": True})

    status = ClarabelOptimizer._require_acceptable_solution(  # pyright: ignore[reportPrivateUsage]  # Contract test pins private solver status validation.
        cast("cp.Problem", cast("object", _Problem("optimal_inaccurate"))),
        cast("cp.Variable", cast("object", _Weights(np.array([0.5])))),
        1.0,
        policy,
    )

    assert status == "optimal_inaccurate"


@pytest.mark.parametrize(
    ("status", "value", "objective", "message"),
    [
        pytest.param("optimal_inaccurate", np.array([0.5]), 1.0, "unacceptable status", id="status"),
        pytest.param("optimal", None, 1.0, "no finite solution", id="missing-weights"),
        pytest.param("optimal", np.array([0.5]), float("nan"), "no finite solution", id="objective"),
    ],
)
def test__require_acceptable_solution_rejects_invalid_solver_result(
    portfolio_policy: PortfolioPolicy,
    status: str,
    value: object | None,
    objective: float,
    message: str,
) -> None:
    with pytest.raises(OptimizationFailedError, match=message):
        _ = ClarabelOptimizer._require_acceptable_solution(  # pyright: ignore[reportPrivateUsage]  # Contract test pins private solver status validation.
            cast("cp.Problem", cast("object", _Problem(status))),
            cast("cp.Variable", cast("object", _Weights(value))),
            objective,
            portfolio_policy,
        )


@pytest.mark.parametrize(
    ("weights", "policy_update", "message"),
    [
        pytest.param([np.nan, 0.5, 0.1], {}, "invalid target", id="non-finite"),
        pytest.param([0.3, 0.6, 0.1], {}, "cash constraint", id="cash"),
        pytest.param([0.7, 0.1, 0.1], {}, "name constraint", id="name"),
        pytest.param([0.36, 0.44, 0.1], {}, "position-change", id="position-change"),
        pytest.param([0.3, 0.25, 0.25], {}, "turnover constraint", id="turnover"),
        pytest.param([0.15, 0.5, 0.1], {"minimum_trade_weight": 0.1}, "minimum-trade", id="trade"),
        pytest.param([0.31, 0.4, 0.1], {}, "satellite constraint", id="satellite"),
        pytest.param([0.1, 0.39, 0.1], {}, "core constraint", id="core"),
        pytest.param([0.31, 0.5, 0.05], {}, "sector constraint", id="sector"),
    ],
)
def test__validate_result_rejects_constraint_violation(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
    weights: list[float],
    policy_update: dict[str, object],
    message: str,
) -> None:
    instruments = tuple(sorted(optimization_input.expected_returns))
    current = np.asarray([optimization_input.current_weights[name] for name in instruments])
    policy = portfolio_policy.model_copy(update=policy_update)

    with pytest.raises(OptimizationResultError, match=message):
        ClarabelOptimizer._validate_result(  # pyright: ignore[reportPrivateUsage]  # Contract test pins private constraint validation.
            instruments,
            np.asarray(weights),
            current,
            optimization_input,
            policy,
        )


def test_optimize_without_satellites_or_matching_extra_sector_remains_supported(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
) -> None:
    inputs = optimization_input.model_copy(update={"satellite_instruments": frozenset()})
    policy = portfolio_policy.model_copy(
        update={
            "minimum_core_weights": {"AAPL": 0.0, "SPY": 0.4, "XOM": 0.0},
            "maximum_sector_weights": {
                **portfolio_policy.maximum_sector_weights,
                "materials": 0.2,
            },
        }
    )

    result = ClarabelOptimizer().optimize(inputs, policy)

    assert set(result.target_weights) == set(inputs.expected_returns)


def test_optimize_without_installed_clarabel_fails_closed(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
    monkeypatch: MonkeyPatch,
) -> None:
    def no_installed_solvers() -> list[str]:
        return []

    monkeypatch.setattr(cp, "installed_solvers", no_installed_solvers)

    with pytest.raises(OptimizerUnavailableError, match="not installed"):
        _ = ClarabelOptimizer().optimize(optimization_input, portfolio_policy)


def test_optimize_with_solver_failure_raises_typed_error(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_solve(_problem: cp.Problem, **_kwargs: object) -> float:
        raise SolverError("solver failed")

    monkeypatch.setattr(cp.Problem, "solve", fail_solve)

    with pytest.raises(OptimizationFailedError, match="failed to solve"):
        _ = ClarabelOptimizer().optimize(optimization_input, portfolio_policy)


def test_optimize_without_solver_package_metadata_fails_closed(
    optimization_input: OptimizationInput,
    portfolio_policy: PortfolioPolicy,
    monkeypatch: MonkeyPatch,
) -> None:
    def missing_version(_distribution: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", missing_version)

    with pytest.raises(OptimizerUnavailableError, match="metadata is unavailable"):
        _ = ClarabelOptimizer().optimize(optimization_input, portfolio_policy)
