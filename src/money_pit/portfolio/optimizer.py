"""Module containing deterministic constrained portfolio optimization."""

from __future__ import annotations

import importlib.metadata
import math
from numbers import Real
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Protocol
from typing import Self
from typing import cast

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from money_pit.portfolio.errors import IncompletePortfolioPolicyError
from money_pit.portfolio.errors import InvalidOptimizationInputError
from money_pit.portfolio.errors import OptimizationFailedError
from money_pit.portfolio.errors import OptimizationResultError
from money_pit.portfolio.errors import OptimizerUnavailableError


if TYPE_CHECKING:
    import cvxpy as cp

    from money_pit.portfolio.policy import PortfolioPolicy


_SOLVER_NAME: str = "CLARABEL"
_SOLVER_DISTRIBUTION: str = "clarabel"
_CANONICAL_DECIMALS: int = 12
_FloatArray = NDArray[np.float64]


class OptimizationInput(BaseModel):
    """Validated point-in-time inputs to one portfolio optimization."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    portfolio_snapshot_id: str = Field(min_length=1)
    market_snapshot_id: str = Field(min_length=1)
    current_weights: dict[str, float]
    expected_returns: dict[str, float]
    covariance: dict[str, dict[str, float]]
    sectors: dict[str, str]
    satellite_instruments: frozenset[str]
    tax_cost_rates: dict[str, float]

    @model_validator(mode="after")
    def validate_instruments(self) -> Self:
        """Reject inconsistent instrument sets before they reach a solver."""
        instruments: set[str] = set(self.expected_returns)
        if not instruments:
            raise ValueError("expected_returns must not be empty")
        required_maps: tuple[tuple[str, set[str]], ...] = (
            ("current_weights", set(self.current_weights)),
            ("covariance rows", set(self.covariance)),
            ("sectors", set(self.sectors)),
            ("tax_cost_rates", set(self.tax_cost_rates)),
        )
        for name, keys in required_maps:
            if keys != instruments:
                raise ValueError(f"{name} must cover exactly the expected-return instruments")
        if not self.satellite_instruments <= instruments:
            raise ValueError("satellite_instruments contains an unknown instrument")
        for instrument, row in self.covariance.items():
            if set(row) != instruments:
                raise ValueError(f"covariance row {instrument!r} is incomplete")
        self.require_finite_inputs()
        if any(weight < 0 or weight > 1 for weight in self.current_weights.values()):
            raise ValueError("current weights must be between zero and one")
        if sum(self.current_weights.values()) > 1 + 1e-9:
            raise ValueError("current weights exceed the portfolio")
        if any(rate < 0 for rate in self.tax_cost_rates.values()):
            raise ValueError("tax cost rates cannot be negative")
        return self

    def require_finite_inputs(self) -> None:
        """Reject non-finite capital-sensitive inputs, including model-copy updates."""
        finite_values: tuple[float, ...] = (
            *self.current_weights.values(),
            *self.expected_returns.values(),
            *self.tax_cost_rates.values(),
        )
        if not all(math.isfinite(value) for value in finite_values):
            raise ValueError("optimization inputs must contain only finite values")


class OptimizationResult(BaseModel):
    """Canonical optimizer output with reproducibility metadata."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    target_weights: dict[str, float]
    cash_weight: float = Field(ge=0, le=1)
    expected_return: float
    expected_risk: float = Field(ge=0)
    turnover: float = Field(ge=0)
    estimated_tax_cost: float = Field(ge=0)
    objective_value: float
    solver_name: str = Field(min_length=1)
    solver_version: str = Field(min_length=1)
    solver_status: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    portfolio_snapshot_id: str = Field(min_length=1)
    market_snapshot_id: str = Field(min_length=1)


class OptimizerBackend(Protocol):
    """Boundary for deterministic portfolio-optimizer implementations."""

    def optimize(
        self,
        optimization_input: OptimizationInput,
        policy: PortfolioPolicy,
    ) -> OptimizationResult:
        """Return a target portfolio or raise a typed fail-closed error."""
        ...


class ClarabelOptimizer:
    """CVXPY backend that explicitly selects the CLARABEL solver."""

    def optimize(
        self,
        optimization_input: OptimizationInput,
        policy: PortfolioPolicy,
    ) -> OptimizationResult:
        """Return a policy-compliant target or raise a typed failure."""
        try:
            optimization_input.require_finite_inputs()
        except ValueError as error:
            raise InvalidOptimizationInputError(str(error)) from error
        self._validate_policy_coverage(optimization_input, policy)
        try:
            import cvxpy as cp
            from cvxpy.error import SolverError
        except ImportError as exc:
            raise OptimizerUnavailableError("cvxpy is required to use ClarabelOptimizer") from exc

        installed_solvers: list[str] = cp.installed_solvers()
        if _SOLVER_NAME not in installed_solvers:
            raise OptimizerUnavailableError("the CLARABEL solver is not installed")

        instruments: tuple[str, ...] = tuple(sorted(optimization_input.expected_returns))
        index_by_instrument: dict[str, int] = {instrument: index for index, instrument in enumerate(instruments)}
        current: _FloatArray = np.asarray(
            [optimization_input.current_weights[instrument] for instrument in instruments],
            dtype=np.float64,
        )
        expected_returns: _FloatArray = np.asarray(
            [optimization_input.expected_returns[instrument] for instrument in instruments],
            dtype=np.float64,
        )
        covariance: _FloatArray = np.asarray(
            [[optimization_input.covariance[row][column] for column in instruments] for row in instruments],
            dtype=np.float64,
        )
        self._validate_covariance(covariance)
        tax_rates: _FloatArray = np.asarray(
            [optimization_input.tax_cost_rates[instrument] for instrument in instruments],
            dtype=np.float64,
        )

        weights: cp.Variable = cp.Variable(len(instruments), nonneg=True)
        delta: cp.Expression = cast("cp.Expression", weights - current)
        turnover: cp.Expression = cast("cp.Expression", 0.5 * (cp.norm1(delta) + cp.abs(cp.sum(delta))))
        estimated_tax_cost: cp.Expression = cast(
            "cp.Expression",
            cp.sum(cp.multiply(tax_rates, cp.pos(-delta))),
        )
        objective_expression: cp.Expression = cast(
            "cp.Expression",
            cp.sum(cp.multiply(expected_returns, weights))
            - policy.risk_aversion * cp.quad_form(weights, cp.psd_wrap(covariance))
            - policy.turnover_penalty * turnover
            - policy.tax_penalty * estimated_tax_cost,
        )
        objective: cp.Maximize = cp.Maximize(objective_expression)
        constraints: list[cp.Constraint] = [
            cast("cp.Constraint", cp.sum(weights) <= 1 - policy.minimum_cash_weight),
            cast("cp.Constraint", weights <= policy.maximum_position_weight),
            cast("cp.Constraint", turnover <= policy.maximum_turnover),
            cast("cp.Constraint", cp.abs(delta) <= policy.maximum_position_change),
        ]
        if optimization_input.satellite_instruments:
            satellite_indices: list[int] = [
                index_by_instrument[instrument] for instrument in sorted(optimization_input.satellite_instruments)
            ]
            constraints.append(
                cast("cp.Constraint", cp.sum(weights[satellite_indices]) <= policy.maximum_satellite_weight)
            )
        for instrument, minimum_weight in sorted(policy.minimum_core_weights.items()):
            constraints.append(cast("cp.Constraint", weights[index_by_instrument[instrument]] >= minimum_weight))
        for sector, maximum_weight in sorted(policy.maximum_sector_weights.items()):
            sector_indices: list[int] = [
                index
                for index, instrument in enumerate(instruments)
                if optimization_input.sectors[instrument] == sector
            ]
            if sector_indices:
                constraints.append(cast("cp.Constraint", cp.sum(weights[sector_indices]) <= maximum_weight))

        problem: cp.Problem = cp.Problem(objective, constraints)
        try:
            raw_objective: object = cast("object", problem.solve(solver=cp.CLARABEL, verbose=False))
        except SolverError as exc:
            raise OptimizationFailedError("CLARABEL failed to solve the portfolio") from exc
        solver_objective_value: float = self._finite_solver_objective(raw_objective)
        solver_status: str = self._require_acceptable_solution(
            problem,
            weights,
            solver_objective_value,
            policy,
        )
        raw_weights: _FloatArray = self._validated_solver_weights(weights, len(instruments))

        canonical_weights: _FloatArray = np.asarray(
            [
                round(max(0.0, float(cast("np.float64", raw_weights[index]))), _CANONICAL_DECIMALS)
                for index in range(raw_weights.size)
            ],
            dtype=np.float64,
        )
        changes: _FloatArray = np.abs(canonical_weights - current)
        canonical_weights = np.where(
            (changes < policy.minimum_trade_weight) & (changes > policy.feasibility_tolerance),
            current,
            canonical_weights,
        )
        self._validate_result(
            instruments,
            canonical_weights,
            current,
            optimization_input,
            policy,
        )
        cash_weight: float = round(
            max(0.0, 1.0 - float(np.sum(canonical_weights))),
            _CANONICAL_DECIMALS,
        )
        risky_weight_changes: _FloatArray = canonical_weights - current
        result_turnover: float = 0.5 * (
            float(cast("np.float64", np.sum(np.abs(risky_weight_changes))))
            + abs(float(cast("np.float64", np.sum(risky_weight_changes))))
        )
        result_tax_cost: float = float(np.sum(tax_rates * np.maximum(current - canonical_weights, 0.0)))
        result_expected_return: float = float(expected_returns @ canonical_weights)
        result_expected_risk: float = float(canonical_weights @ covariance @ canonical_weights)
        result_objective_value: float = (
            result_expected_return
            - policy.risk_aversion * result_expected_risk
            - policy.turnover_penalty * result_turnover
            - policy.tax_penalty * result_tax_cost
        )
        try:
            solver_version: str = importlib.metadata.version(_SOLVER_DISTRIBUTION)
        except importlib.metadata.PackageNotFoundError as exc:
            raise OptimizerUnavailableError("CLARABEL package metadata is unavailable") from exc
        return OptimizationResult(
            target_weights={
                instrument: float(cast("np.float64", canonical_weights[index]))
                for index, instrument in enumerate(instruments)
            },
            cash_weight=cash_weight,
            expected_return=result_expected_return,
            expected_risk=max(0.0, result_expected_risk),
            turnover=result_turnover,
            estimated_tax_cost=result_tax_cost,
            objective_value=result_objective_value,
            solver_name=_SOLVER_NAME,
            solver_version=solver_version,
            solver_status=solver_status,
            policy_version=policy.policy_version,
            portfolio_snapshot_id=optimization_input.portfolio_snapshot_id,
            market_snapshot_id=optimization_input.market_snapshot_id,
        )

    @staticmethod
    def _finite_solver_objective(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise OptimizationFailedError("CLARABEL returned no finite objective")
        objective_value: float = float(value)
        if not math.isfinite(objective_value):
            raise OptimizationFailedError("CLARABEL returned no finite objective")
        return objective_value

    @staticmethod
    def _require_acceptable_solution(
        problem: cp.Problem,
        weights: cp.Variable,
        objective_value: float,
        policy: PortfolioPolicy,
    ) -> str:
        solver_status: str = str(problem.status)
        accepted_statuses: set[str] = {"optimal"}
        if policy.accept_optimal_inaccurate:
            accepted_statuses.add("optimal_inaccurate")
        if solver_status not in accepted_statuses:
            raise OptimizationFailedError(f"CLARABEL returned unacceptable status {solver_status!r}")
        raw_value: object = cast("object", weights.value)
        if raw_value is None or not math.isfinite(objective_value):
            raise OptimizationFailedError("CLARABEL returned no finite solution")
        return solver_status

    @staticmethod
    def _validated_solver_weights(weights: cp.Variable, expected_count: int) -> _FloatArray:
        raw_value: object = cast("object", weights.value)
        try:
            raw_weights: _FloatArray = np.asarray(raw_value, dtype=np.float64).reshape(-1)
        except (TypeError, ValueError) as error:
            raise OptimizationFailedError("CLARABEL returned malformed weights") from error
        if raw_weights.shape != (expected_count,) or not np.all(np.isfinite(raw_weights)):
            raise OptimizationFailedError("CLARABEL returned malformed weights")
        return raw_weights

    @staticmethod
    def _validate_policy_coverage(
        optimization_input: OptimizationInput,
        policy: PortfolioPolicy,
    ) -> None:
        instruments: set[str] = set(optimization_input.expected_returns)
        missing_core: set[str] = set(policy.minimum_core_weights) - instruments
        if missing_core:
            raise IncompletePortfolioPolicyError(f"core policy names unknown instruments: {sorted(missing_core)}")
        sectors: set[str] = set(optimization_input.sectors.values())
        missing_sectors: set[str] = sectors - set(policy.maximum_sector_weights)
        if missing_sectors:
            raise IncompletePortfolioPolicyError(f"sector policy is incomplete: {sorted(missing_sectors)}")
        non_satellite_non_core: set[str] = (
            instruments - optimization_input.satellite_instruments - set(policy.minimum_core_weights)
        )
        if non_satellite_non_core:
            raise IncompletePortfolioPolicyError(
                f"every instrument must be classified as core or satellite: {sorted(non_satellite_non_core)}"
            )

    @staticmethod
    def _validate_covariance(covariance: _FloatArray) -> None:
        if not np.all(np.isfinite(covariance)):
            raise InvalidOptimizationInputError("covariance contains a non-finite value")
        if not np.allclose(covariance, covariance.T, rtol=1e-9, atol=1e-12):
            raise InvalidOptimizationInputError("covariance must be symmetric")
        eigenvalues: _FloatArray = np.asarray(
            np.linalg.eigvalsh(covariance),
            dtype=np.float64,
        )
        if float(np.min(eigenvalues)) < -1e-10:
            raise InvalidOptimizationInputError("covariance must be positive semidefinite")

    @staticmethod
    def _validate_result(
        instruments: tuple[str, ...],
        weights: _FloatArray,
        current: _FloatArray,
        optimization_input: OptimizationInput,
        policy: PortfolioPolicy,
    ) -> None:
        tolerance: float = policy.feasibility_tolerance
        if not np.all(np.isfinite(weights)) or np.any(weights < -tolerance):
            raise OptimizationResultError("solver returned invalid target weights")
        if float(np.sum(weights)) > 1 - policy.minimum_cash_weight + tolerance:
            raise OptimizationResultError("solver result violates the cash constraint")
        if float(np.max(weights)) > policy.maximum_position_weight + tolerance:
            raise OptimizationResultError("solver result violates the name constraint")
        changes: _FloatArray = np.abs(weights - current)
        if float(np.max(changes)) > policy.maximum_position_change + tolerance:
            raise OptimizationResultError("solver result violates the position-change constraint")
        full_turnover: float = 0.5 * (float(np.sum(changes)) + abs(float(np.sum(weights - current))))
        if full_turnover > policy.maximum_turnover + tolerance:
            raise OptimizationResultError("solver result violates the turnover constraint")
        if any(
            tolerance < float(cast("np.float64", changes[index])) < policy.minimum_trade_weight - tolerance
            for index in range(changes.size)
        ):
            raise OptimizationResultError("solver result violates the minimum-trade constraint")
        ClarabelOptimizer._validate_exposure_result(
            instruments,
            weights,
            optimization_input,
            policy,
        )

    @staticmethod
    def _validate_exposure_result(
        instruments: tuple[str, ...],
        weights: _FloatArray,
        optimization_input: OptimizationInput,
        policy: PortfolioPolicy,
    ) -> None:
        tolerance: float = policy.feasibility_tolerance
        satellite_weight: float = sum(
            float(cast("np.float64", weights[index]))
            for index, instrument in enumerate(instruments)
            if instrument in optimization_input.satellite_instruments
        )
        if satellite_weight > policy.maximum_satellite_weight + tolerance:
            raise OptimizationResultError("solver result violates the satellite constraint")
        for instrument, minimum_weight in policy.minimum_core_weights.items():
            index: int = instruments.index(instrument)
            if float(cast("np.float64", weights[index])) < minimum_weight - tolerance:
                raise OptimizationResultError("solver result violates a core constraint")
        for sector, maximum_weight in policy.maximum_sector_weights.items():
            sector_weight: float = sum(
                float(cast("np.float64", weights[index]))
                for index, instrument in enumerate(instruments)
                if optimization_input.sectors[instrument] == sector
            )
            if sector_weight > maximum_weight + tolerance:
                raise OptimizationResultError("solver result violates a sector constraint")
