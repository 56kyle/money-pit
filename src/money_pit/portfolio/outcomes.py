"""Module containing deterministic, non-training investment outcome evaluation."""

import hashlib
import json
import math
from datetime import datetime
from typing import ClassVar
from typing import Protocol

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator

from money_pit.portfolio.repository import SnapshotRepository
from money_pit.portfolio.theses import ThesisRepository
from money_pit.schemas.outcomes import OutcomeBoundary
from money_pit.schemas.outcomes import OutcomeMetric
from money_pit.schemas.outcomes import OutcomeSchedule
from money_pit.schemas.portfolio_plan import PortfolioPlan


class OutcomeError(Exception):
    """Base class for deterministic outcome scheduling and evaluation failures."""


class OutcomeScheduleBindingError(OutcomeError):
    """Raised when durable planning inputs cannot form the configured outcome binding."""


class OutcomeBindingMismatchError(OutcomeError):
    """Raised when observed inputs do not match the exact scheduled identities."""


class OutcomeIntervalMismatchError(OutcomeError):
    """Raised when observation points fall outside the exact evaluation interval."""


class OutcomeBoundaryIncompleteError(OutcomeError):
    """Raised when evaluation or its observation series does not reach the boundary."""


class OutcomeScenarioInputError(OutcomeError):
    """Raised when scenario attribution inputs are inconsistent or unnormalized."""


class OutcomeMetricInputError(OutcomeError):
    """Raised when a metric numerator and denominator cannot represent a ratio."""


class ScenarioForecast(BaseModel):
    """Probability assigned to one mutually exclusive realized outcome."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    name: str = Field(min_length=1)
    probability: float = Field(ge=0, le=1)
    expected_return: float


class BenchmarkBinding(BaseModel):
    """Explicit configured benchmark identity, provider, composition, and weights."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    benchmark_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    weights: dict[str, float] = Field(min_length=1)

    @model_validator(mode="after")
    def require_normalized_weights(self) -> "BenchmarkBinding":
        """Require a complete normalized long-only benchmark composition."""
        if any(weight <= 0 or weight > 1 for weight in self.weights.values()) or not math.isclose(
            sum(self.weights.values()), 1.0, abs_tol=1e-9
        ):
            raise ValueError("benchmark weights must be positive and sum to one")
        return self

    def composition_hash(self) -> str:
        """Return the canonical configured benchmark binding hash."""
        return _stable_hash(self.model_dump(mode="json"))


class OutcomeSeriesPoint(BaseModel):
    """One immutable timestamped portfolio or benchmark observation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    observed_at: AwareDatetime
    value: float = Field(gt=0)


class OutcomeObservationSeries(BaseModel):
    """Content-addressed provider result for one scheduled observation query."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    provider: str = Field(min_length=1)
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_input_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    series_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    points: tuple[OutcomeSeriesPoint, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def require_canonical_series(self) -> "OutcomeObservationSeries":
        """Require ordered unique observations and their exact content hash."""
        times = tuple(point.observed_at for point in self.points)
        if times != tuple(sorted(times)) or len(times) != len(set(times)):
            raise ValueError("outcome series observations must have unique ascending timestamps")
        if self.series_snapshot_hash != self.fingerprint():
            raise ValueError("outcome series snapshot hash does not match its content")
        return self

    def fingerprint(self) -> str:
        """Return the canonical hash excluding the self-authenticating field."""
        return _stable_hash(
            {
                "provider": self.provider,
                "query_hash": self.query_hash,
                "baseline_input_snapshot_hash": self.baseline_input_snapshot_hash,
                "points": [point.model_dump(mode="json") for point in self.points],
            }
        )


class OutcomeInputs(BaseModel):
    """Observed values needed for one deterministic evaluation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    observed_at: AwareDatetime
    scenario_forecasts: tuple[ScenarioForecast, ...] = Field(min_length=1)
    invalidation_was_expected: bool
    thesis_invalidated: bool
    portfolio_observations: OutcomeObservationSeries
    benchmark_observations: OutcomeObservationSeries
    turnover: float = Field(ge=0)
    estimated_slippage: float = Field(ge=0)
    realized_slippage: float = Field(ge=0)
    estimated_tax_cost: float = Field(ge=0)
    realized_tax_cost: float | None = Field(default=None, ge=0)
    source_supported_claims: int = Field(ge=0)
    source_evaluated_claims: int = Field(ge=0)
    provider_useful_results: int = Field(ge=0)
    provider_total_results: int = Field(ge=0)
    unresolved_research_tasks: int = Field(ge=0)
    total_research_tasks: int = Field(ge=0)
    agent_recommendation_return: float
    prompt_versions: dict[str, str] = Field(min_length=1)
    model_versions: dict[str, str] = Field(min_length=1)


class InvestmentOutcomeMetrics(BaseModel):
    """Version-bound evaluation metrics with no policy-mutation authority."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    schedule_id: str = Field(min_length=1)
    thesis_revision_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    benchmark_snapshot_id: str = Field(min_length=1)
    evaluated_at: AwareDatetime
    scenario_brier_score: float = Field(ge=0)
    invalidation_accuracy: float = Field(ge=0, le=1)
    excess_return: float
    drawdown: float = Field(ge=0, le=1)
    turnover: float = Field(ge=0)
    slippage_error: float
    tax_cost_error: float | None
    source_reliability: float | None = Field(default=None, ge=0, le=1)
    provider_usefulness: float | None = Field(default=None, ge=0, le=1)
    unresolved_rate: float | None = Field(default=None, ge=0, le=1)
    agent_recommendation_return: float
    prompt_versions: dict[str, str] = Field(min_length=1)
    model_versions: dict[str, str] = Field(min_length=1)


class OutcomeRepository(Protocol):
    """Persistence boundary for scheduled observations and immutable metrics."""

    def append_schedule(self, schedule: OutcomeSchedule) -> None:
        """Persist one immutable observation schedule."""
        ...

    def due(self, *, as_of: datetime) -> tuple[OutcomeSchedule, ...]:
        """Return unobserved schedules due at the point-in-time cutoff."""
        ...

    def append_metrics(self, metrics: OutcomeMetric) -> None:
        """Persist one immutable version-bound evaluation."""
        ...


class PlanOutcomeScheduler:
    """Schedule exact thesis and plan versions at their economic boundaries."""

    def __init__(
        self,
        repository: OutcomeRepository,
        theses: ThesisRepository,
        snapshots: SnapshotRepository,
        benchmark: BenchmarkBinding,
    ) -> None:
        """Bind immutable outcome and thesis repositories."""
        self._repository: OutcomeRepository = repository
        self._theses: ThesisRepository = theses
        self._snapshots: SnapshotRepository = snapshots
        self._benchmark: BenchmarkBinding = benchmark

    def schedule(self, plan: PortfolioPlan, thesis_revision_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Persist deterministic event, review, and horizon observations."""
        schedules: list[OutcomeSchedule] = []
        for revision in self._theses.revisions_by_ids(tuple(sorted(set(thesis_revision_ids)))):
            market = self._snapshots.get_market(plan.payload.market_snapshot_id)
            portfolio = self._snapshots.get_portfolio(plan.payload.portfolio_snapshot_id)
            quote_by_instrument = {quote.instrument: quote for quote in market.payload.quotes}
            if set(self._benchmark.weights) - set(quote_by_instrument):
                raise OutcomeScheduleBindingError("market snapshot does not cover the configured benchmark")
            if any(
                quote_by_instrument[instrument].source != self._benchmark.provider
                for instrument in self._benchmark.weights
            ):
                raise OutcomeScheduleBindingError(
                    "market snapshot provider does not match the configured benchmark provider"
                )
            benchmark_composition_hash = self._benchmark.composition_hash()
            portfolio_baseline_value = portfolio.payload.available_cash + sum(
                position.market_value for position in portfolio.payload.positions
            )
            benchmark_baseline_value = sum(
                self._benchmark.weights[instrument] * quote_by_instrument[instrument].price
                for instrument in self._benchmark.weights
            )
            scenario_distribution_hash = _stable_hash(
                [item.model_dump(mode="json") for item in revision.scenario_distribution]
            )
            boundaries: tuple[tuple[OutcomeBoundary, datetime | None], ...] = (
                (OutcomeBoundary.EVENT, revision.event_at),
                (OutcomeBoundary.REVIEW, revision.review_at),
                (OutcomeBoundary.HORIZON, revision.valid_until),
            )
            for boundary, observe_at in boundaries:
                if observe_at is None:
                    continue
                identity = hashlib.sha256(
                    (
                        f"{revision.revision_id}\0{plan.payload.plan_id}\0{plan.plan_hash}"
                        f"\0{boundary.value}\0{observe_at.isoformat()}\0{scenario_distribution_hash}"
                        f"\0{market.snapshot_id}\0{benchmark_composition_hash}"
                    ).encode(),
                ).hexdigest()
                schedule = OutcomeSchedule(
                    schedule_id=f"outcome:{identity}",
                    thesis_revision_id=revision.revision_id,
                    plan_id=plan.payload.plan_id,
                    plan_hash=plan.plan_hash,
                    benchmark_snapshot_id=plan.payload.market_snapshot_id,
                    benchmark_id=self._benchmark.benchmark_id,
                    benchmark_provider=self._benchmark.provider,
                    benchmark_weights=self._benchmark.weights,
                    benchmark_composition_hash=benchmark_composition_hash,
                    benchmark_input_snapshot_hash=market.snapshot_id,
                    portfolio_input_snapshot_hash=plan.payload.portfolio_snapshot_id,
                    portfolio_observation_provider=f"broker:{plan.payload.broker_environment.value}",
                    portfolio_observation_query_hash=_stable_hash(
                        {
                            "account_id": plan.payload.account_id,
                            "baseline_snapshot": plan.payload.portfolio_snapshot_id,
                            "observe_at": observe_at.isoformat(),
                            "metric": "total_portfolio_value",
                        }
                    ),
                    benchmark_observation_query_hash=_stable_hash(
                        {
                            "baseline_snapshot": market.snapshot_id,
                            "benchmark_id": self._benchmark.benchmark_id,
                            "weights": self._benchmark.weights,
                            "observe_at": observe_at.isoformat(),
                            "metric": "total_return",
                        }
                    ),
                    portfolio_baseline_observed_at=portfolio.payload.captured_at,
                    benchmark_baseline_observed_at=market.payload.captured_at,
                    portfolio_baseline_value=portfolio_baseline_value,
                    benchmark_baseline_value=benchmark_baseline_value,
                    scenario_distribution_hash=scenario_distribution_hash,
                    prompt_versions=plan.payload.prompt_versions,
                    model_versions=plan.payload.model_versions,
                    boundary=boundary,
                    observe_at=observe_at,
                    schedule_details={
                        "horizon_class": revision.horizon_class.value,
                        "thesis_id": revision.thesis_id,
                    },
                )
                self._repository.append_schedule(schedule)
                schedules.append(schedule)
        return tuple(schedule.schedule_id for schedule in schedules)


def evaluate_outcome(schedule: OutcomeSchedule, inputs: OutcomeInputs) -> InvestmentOutcomeMetrics:
    """Calculate calibration, portfolio, source, provider, and agent metrics."""
    if inputs.observed_at < schedule.observe_at:
        raise OutcomeBoundaryIncompleteError("outcome cannot be evaluated before its scheduled boundary")
    if schedule.plan_id is None or schedule.plan_hash is None:
        raise OutcomeBindingMismatchError("portfolio outcome evaluation requires an exact plan binding")
    input_scenario_hash = _stable_hash([item.model_dump(mode="json") for item in inputs.scenario_forecasts])
    exact_bindings = (
        input_scenario_hash == schedule.scenario_distribution_hash,
        inputs.prompt_versions == schedule.prompt_versions,
        inputs.model_versions == schedule.model_versions,
        inputs.portfolio_observations.provider == schedule.portfolio_observation_provider,
        inputs.portfolio_observations.query_hash == schedule.portfolio_observation_query_hash,
        inputs.portfolio_observations.baseline_input_snapshot_hash == schedule.portfolio_input_snapshot_hash,
        inputs.benchmark_observations.provider == schedule.benchmark_provider,
        inputs.benchmark_observations.query_hash == schedule.benchmark_observation_query_hash,
        inputs.benchmark_observations.baseline_input_snapshot_hash == schedule.benchmark_input_snapshot_hash,
    )
    if not all(exact_bindings):
        raise OutcomeBindingMismatchError("outcome inputs do not match the durable scheduled bindings")
    if (
        inputs.portfolio_observations.points[0].observed_at != schedule.portfolio_baseline_observed_at
        or inputs.benchmark_observations.points[0].observed_at != schedule.benchmark_baseline_observed_at
        or inputs.portfolio_observations.points[-1].observed_at > inputs.observed_at
        or inputs.benchmark_observations.points[-1].observed_at > inputs.observed_at
        or any(point.observed_at > inputs.observed_at for point in inputs.portfolio_observations.points)
        or any(point.observed_at > inputs.observed_at for point in inputs.benchmark_observations.points)
    ):
        raise OutcomeIntervalMismatchError("outcome observation series fall outside the exact evaluation interval")
    if not math.isclose(
        inputs.portfolio_observations.points[0].value,
        schedule.portfolio_baseline_value,
        rel_tol=1e-12,
    ) or not math.isclose(
        inputs.benchmark_observations.points[0].value,
        schedule.benchmark_baseline_value,
        rel_tol=1e-12,
    ):
        raise OutcomeBindingMismatchError("outcome series baseline values do not match the plan-bound snapshots")
    if (
        inputs.portfolio_observations.points[-1].observed_at < schedule.observe_at
        or inputs.benchmark_observations.points[-1].observed_at < schedule.observe_at
    ):
        raise OutcomeBoundaryIncompleteError("outcome observation series do not reach the scheduled boundary")
    names: tuple[str, ...] = tuple(item.name for item in inputs.scenario_forecasts)
    if len(names) != len(set(names)):
        raise OutcomeScenarioInputError("scenario forecasts must have unique names")
    total_probability: float = sum(item.probability for item in inputs.scenario_forecasts)
    if not math.isclose(total_probability, 1.0, abs_tol=1e-9):
        raise OutcomeScenarioInputError("scenario probabilities must sum to one")
    portfolio_values = tuple(point.value for point in inputs.portfolio_observations.points)
    benchmark_values = tuple(point.value for point in inputs.benchmark_observations.points)
    portfolio_return = portfolio_values[-1] / portfolio_values[0] - 1
    benchmark_return = benchmark_values[-1] / benchmark_values[0] - 1
    realized_scenario = min(
        inputs.scenario_forecasts,
        key=lambda item: (abs(item.expected_return - portfolio_return), item.name),
    ).name
    brier_score: float = sum(
        (item.probability - (1.0 if item.name == realized_scenario else 0.0)) ** 2 for item in inputs.scenario_forecasts
    )
    peak_portfolio_value = max(portfolio_values)
    drawdown: float = (peak_portfolio_value - portfolio_values[-1]) / peak_portfolio_value
    return InvestmentOutcomeMetrics(
        schedule_id=schedule.schedule_id,
        thesis_revision_id=schedule.thesis_revision_id,
        plan_id=schedule.plan_id,
        plan_hash=schedule.plan_hash,
        benchmark_snapshot_id=schedule.benchmark_snapshot_id,
        evaluated_at=inputs.observed_at,
        scenario_brier_score=brier_score,
        invalidation_accuracy=1.0 if inputs.thesis_invalidated == inputs.invalidation_was_expected else 0.0,
        excess_return=portfolio_return - benchmark_return,
        drawdown=drawdown,
        turnover=inputs.turnover,
        slippage_error=inputs.realized_slippage - inputs.estimated_slippage,
        tax_cost_error=(
            None if inputs.realized_tax_cost is None else inputs.realized_tax_cost - inputs.estimated_tax_cost
        ),
        source_reliability=_ratio(inputs.source_supported_claims, inputs.source_evaluated_claims),
        provider_usefulness=_ratio(inputs.provider_useful_results, inputs.provider_total_results),
        unresolved_rate=_ratio(inputs.unresolved_research_tasks, inputs.total_research_tasks),
        agent_recommendation_return=inputs.agent_recommendation_return,
        prompt_versions=inputs.prompt_versions,
        model_versions=inputs.model_versions,
    )


def to_outcome_metric(
    metrics: InvestmentOutcomeMetrics,
    *,
    metric_id: str,
) -> OutcomeMetric:
    """Convert calculated metrics into the shared immutable persistence contract."""
    prompt_versions: dict[str, JsonValue] = dict(metrics.prompt_versions)
    model_versions: dict[str, JsonValue] = dict(metrics.model_versions)
    metric_values: dict[str, JsonValue] = {
        "scenario_brier_score": metrics.scenario_brier_score,
        "invalidation_accuracy": metrics.invalidation_accuracy,
        "excess_return": metrics.excess_return,
        "drawdown": metrics.drawdown,
        "turnover": metrics.turnover,
        "slippage_error": metrics.slippage_error,
        "tax_cost_error": metrics.tax_cost_error,
        "source_reliability": metrics.source_reliability,
        "provider_usefulness": metrics.provider_usefulness,
        "unresolved_rate": metrics.unresolved_rate,
        "agent_recommendation_return": metrics.agent_recommendation_return,
        "prompt_versions": prompt_versions,
        "model_versions": model_versions,
    }
    return OutcomeMetric(
        metric_id=metric_id,
        schedule_id=metrics.schedule_id,
        thesis_revision_id=metrics.thesis_revision_id,
        plan_id=metrics.plan_id,
        plan_hash=metrics.plan_hash,
        benchmark_snapshot_id=metrics.benchmark_snapshot_id,
        evaluated_at=metrics.evaluated_at,
        metrics=metric_values,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    if numerator > denominator:
        raise OutcomeMetricInputError("metric numerator cannot exceed its denominator")
    return numerator / denominator


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
