import hashlib
import json
import math
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from pydantic import ValidationError

from money_pit.portfolio.outcome_repository import SqliteOutcomeRepository
from money_pit.portfolio.outcomes import BenchmarkBinding
from money_pit.portfolio.outcomes import OutcomeBoundaryIncompleteError
from money_pit.portfolio.outcomes import OutcomeError
from money_pit.portfolio.outcomes import OutcomeInputs
from money_pit.portfolio.outcomes import OutcomeIntervalMismatchError
from money_pit.portfolio.outcomes import OutcomeObservationSeries
from money_pit.portfolio.outcomes import OutcomeSeriesPoint
from money_pit.portfolio.outcomes import PlanOutcomeScheduler
from money_pit.portfolio.outcomes import ScenarioForecast
from money_pit.portfolio.outcomes import evaluate_outcome
from money_pit.portfolio.repository import SnapshotRepository
from money_pit.portfolio.snapshots import MarketQuote
from money_pit.portfolio.snapshots import MarketStatePayload
from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.portfolio.theses import ThesisRepository
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.outcomes import OutcomeBoundary
from money_pit.schemas.outcomes import OutcomeSchedule
from money_pit.schemas.portfolio_plan import PlanTaxEstimate
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.theses import CandidateStatus
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ScenarioOutcome
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.theses import ThesisRevision
from money_pit.schemas.theses import ThesisStatus
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.schemas.universe import UniverseLayer
from money_pit.storage.database import Database


def _observation_series(
    *,
    provider: str,
    query_hash: str,
    baseline_hash: str,
    observed_at: datetime,
    values: tuple[float, ...],
) -> OutcomeObservationSeries:
    points = tuple(
        OutcomeSeriesPoint(observed_at=observed_at - timedelta(days=len(values) - index - 1), value=value)
        for index, value in enumerate(values)
    )
    provisional = OutcomeObservationSeries.model_construct(
        provider=provider,
        query_hash=query_hash,
        baseline_input_snapshot_hash=baseline_hash,
        series_snapshot_hash="0" * 64,
        points=points,
    )
    return OutcomeObservationSeries(
        provider=provider,
        query_hash=query_hash,
        baseline_input_snapshot_hash=baseline_hash,
        series_snapshot_hash=provisional.fingerprint(),
        points=points,
    )


def _valid_outcome_case(observed_at: datetime) -> tuple[OutcomeSchedule, OutcomeInputs]:
    forecasts = (ScenarioForecast(name="base", probability=1.0, expected_return=0.08),)
    scenario_hash = hashlib.sha256(
        json.dumps(
            [item.model_dump(mode="json") for item in forecasts],
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    benchmark = BenchmarkBinding(
        benchmark_id="configured-benchmark",
        provider="point-in-time-market",
        weights={"SPY": 1.0},
    )
    schedule = OutcomeSchedule(
        schedule_id="schedule-interval",
        thesis_revision_id="revision-3",
        plan_id="plan-7",
        plan_hash="a" * 64,
        benchmark_snapshot_id="benchmark-snapshot-2",
        benchmark_id=benchmark.benchmark_id,
        benchmark_provider=benchmark.provider,
        benchmark_weights=benchmark.weights,
        benchmark_composition_hash=benchmark.composition_hash(),
        benchmark_input_snapshot_hash="b" * 64,
        portfolio_input_snapshot_hash="c" * 64,
        portfolio_observation_provider="paper-broker",
        portfolio_observation_query_hash="d" * 64,
        benchmark_observation_query_hash="e" * 64,
        portfolio_baseline_observed_at=observed_at - timedelta(days=2),
        benchmark_baseline_observed_at=observed_at - timedelta(days=1),
        portfolio_baseline_value=100,
        benchmark_baseline_value=100,
        scenario_distribution_hash=scenario_hash,
        prompt_versions={"synthesis": "synthesis-v4"},
        model_versions={"synthesis": "model-v2"},
        boundary=OutcomeBoundary.REVIEW,
        observe_at=observed_at,
    )
    inputs = OutcomeInputs(
        observed_at=observed_at,
        scenario_forecasts=forecasts,
        invalidation_was_expected=False,
        thesis_invalidated=False,
        portfolio_observations=_observation_series(
            provider="paper-broker",
            query_hash="d" * 64,
            baseline_hash="c" * 64,
            observed_at=observed_at,
            values=(100, 104, 108),
        ),
        benchmark_observations=_observation_series(
            provider="point-in-time-market",
            query_hash="e" * 64,
            baseline_hash="b" * 64,
            observed_at=observed_at,
            values=(100, 105),
        ),
        turnover=0.1,
        estimated_slippage=0.001,
        realized_slippage=0.002,
        estimated_tax_cost=5.0,
        realized_tax_cost=4.0,
        source_supported_claims=2,
        source_evaluated_claims=3,
        provider_useful_results=1,
        provider_total_results=2,
        unresolved_research_tasks=1,
        total_research_tasks=4,
        agent_recommendation_return=0.07,
        prompt_versions={"synthesis": "synthesis-v4"},
        model_versions={"synthesis": "model-v2"},
    )
    return schedule, inputs


@pytest.mark.parametrize(
    ("invalid_axis", "expected_error"),
    [
        ("wrong_start", OutcomeIntervalMismatchError),
        ("post_observed", OutcomeIntervalMismatchError),
        ("missing_boundary", OutcomeBoundaryIncompleteError),
    ],
)
def test_evaluate_outcome_rejects_series_outside_exact_interval(
    invalid_axis: str,
    expected_error: type[OutcomeError],
) -> None:
    observed_at = datetime(2026, 11, 1, tzinfo=UTC)
    schedule, inputs = _valid_outcome_case(observed_at)
    if invalid_axis == "wrong_start":
        portfolio = inputs.portfolio_observations.model_copy(
            update={
                "points": (
                    inputs.portfolio_observations.points[0].model_copy(
                        update={"observed_at": observed_at - timedelta(days=3)}
                    ),
                    inputs.portfolio_observations.points[1],
                )
            }
        )
    elif invalid_axis == "post_observed":
        portfolio = inputs.portfolio_observations.model_copy(
            update={
                "points": (
                    inputs.portfolio_observations.points[0],
                    inputs.portfolio_observations.points[1].model_copy(
                        update={"observed_at": observed_at + timedelta(seconds=1)}
                    ),
                )
            }
        )
    else:
        portfolio = inputs.portfolio_observations.model_copy(
            update={
                "points": (
                    inputs.portfolio_observations.points[0],
                    inputs.portfolio_observations.points[1].model_copy(
                        update={"observed_at": observed_at - timedelta(seconds=1)}
                    ),
                )
            }
        )

    with pytest.raises(expected_error):
        _ = evaluate_outcome(schedule, inputs.model_copy(update={"portfolio_observations": portfolio}))


def test_outcome_schedule_rejects_benchmark_composition_hash_mismatch() -> None:
    schedule, _ = _valid_outcome_case(datetime(2026, 11, 1, tzinfo=UTC))

    with pytest.raises(ValidationError):
        _ = OutcomeSchedule.model_validate({**schedule.model_dump(mode="json"), "benchmark_composition_hash": "0" * 64})


def test_evaluate_outcome_preserves_exact_thesis_and_plan_version_bindings() -> None:
    observed_at = datetime(2026, 11, 1, tzinfo=UTC)
    forecasts = (ScenarioForecast(name="base", probability=1.0, expected_return=0.08),)
    scenario_hash = hashlib.sha256(
        json.dumps(
            [item.model_dump(mode="json") for item in forecasts],
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    benchmark = BenchmarkBinding(
        benchmark_id="configured-benchmark",
        provider="point-in-time-market",
        weights={"SPY": 1.0},
    )
    schedule = OutcomeSchedule(
        schedule_id="schedule-1",
        thesis_revision_id="revision-3",
        plan_id="plan-7",
        plan_hash="a" * 64,
        benchmark_snapshot_id="benchmark-snapshot-2",
        benchmark_id="configured-benchmark",
        benchmark_provider="point-in-time-market",
        benchmark_composition_hash=benchmark.composition_hash(),
        benchmark_weights=benchmark.weights,
        benchmark_input_snapshot_hash="b" * 64,
        portfolio_input_snapshot_hash="c" * 64,
        portfolio_observation_provider="paper-broker",
        portfolio_observation_query_hash="d" * 64,
        benchmark_observation_query_hash="e" * 64,
        portfolio_baseline_observed_at=observed_at - timedelta(days=2),
        benchmark_baseline_observed_at=observed_at - timedelta(days=1),
        portfolio_baseline_value=100,
        benchmark_baseline_value=100,
        scenario_distribution_hash=scenario_hash,
        prompt_versions={"synthesis": "synthesis-v4"},
        model_versions={"synthesis": "model-v2"},
        boundary=OutcomeBoundary.REVIEW,
        observe_at=observed_at,
    )
    inputs = OutcomeInputs(
        observed_at=observed_at,
        scenario_forecasts=forecasts,
        invalidation_was_expected=False,
        thesis_invalidated=False,
        portfolio_observations=_observation_series(
            provider="paper-broker",
            query_hash="d" * 64,
            baseline_hash="c" * 64,
            observed_at=observed_at,
            values=(100, 110, 108),
        ),
        benchmark_observations=_observation_series(
            provider="point-in-time-market",
            query_hash="e" * 64,
            baseline_hash="b" * 64,
            observed_at=observed_at,
            values=(100, 105),
        ),
        turnover=0.1,
        estimated_slippage=0.001,
        realized_slippage=0.002,
        estimated_tax_cost=5.0,
        realized_tax_cost=4.0,
        source_supported_claims=2,
        source_evaluated_claims=3,
        provider_useful_results=1,
        provider_total_results=2,
        unresolved_research_tasks=1,
        total_research_tasks=4,
        agent_recommendation_return=0.07,
        prompt_versions={"synthesis": "synthesis-v4"},
        model_versions={"synthesis": "model-v2"},
    )

    outcome = evaluate_outcome(schedule, inputs)

    assert (
        outcome.thesis_revision_id,
        outcome.plan_id,
        outcome.plan_hash,
        outcome.prompt_versions,
        outcome.model_versions,
    ) == (
        "revision-3",
        "plan-7",
        "a" * 64,
        {"synthesis": "synthesis-v4"},
        {"synthesis": "model-v2"},
    )
    assert math.isclose(outcome.excess_return, 0.03)
    assert math.isclose(outcome.drawdown, 2 / 110)


def test_plan_outcome_scheduler_persists_each_exact_economic_boundary(database: Database) -> None:
    known_at = datetime(2026, 8, 9, tzinfo=UTC)
    event_at = known_at + timedelta(days=7)
    review_at = known_at + timedelta(days=30)
    valid_until = known_at + timedelta(days=90)
    theses = ThesisRepository(database)
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-new",
        subject="NEW backlog conversion",
        direction=ThesisDirection.LONG,
        instrument="NEW",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(
            universe_layer=UniverseLayer.WATCHLIST,
            universe_reference="NEW",
        ),
        status=CandidateStatus.PROMOTED,
        created_at=known_at,
        known_at=known_at,
    )
    revision = ThesisRevision(
        revision_id="revision-new-1",
        thesis_id="thesis-new",
        revision_number=1,
        promoted_from_candidate_id=candidate.candidate_thesis_id,
        subject=candidate.subject,
        instrument="NEW",
        direction=ThesisDirection.LONG,
        status=ThesisStatus.ACTIVE,
        horizon_class=HorizonClass.TACTICAL,
        effective_from=known_at,
        event_at=event_at,
        review_at=review_at,
        valid_until=valid_until,
        scenario_distribution=(ScenarioOutcome(name="base", probability=1, expected_return=0.08),),
        invalidation_rules=("Backlog conversion falls below plan.",),
        causal_mechanisms=("backlog conversion",),
        confidence=0.7,
        reasoning="Primary evidence supports the material premise.",
        created_at=known_at,
        known_at=known_at,
    )
    theses.append_candidate(candidate)
    theses.append_revision(revision)
    market = MarketStateSnapshot.from_payload(
        MarketStatePayload(
            captured_at=known_at,
            quotes=(
                MarketQuote(
                    instrument="NEW",
                    price=100,
                    observed_at=known_at,
                    source="point-in-time-market",
                ),
            ),
        )
    )
    snapshots = SnapshotRepository(database)
    snapshots.append_market(market)
    portfolio = PortfolioStateSnapshot.from_payload(
        PortfolioStatePayload(
            account_id="paper-account",
            captured_at=known_at,
            available_cash=10_000,
            positions=(),
            open_order_ids=(),
        )
    )
    snapshots.append_portfolio(portfolio)
    plan = PortfolioPlan.from_payload(
        PortfolioPlanPayload(
            plan_id="plan-new",
            created_at=known_at,
            expires_at=known_at + timedelta(minutes=30),
            portfolio_snapshot_id=portfolio.snapshot_id,
            market_snapshot_id=market.snapshot_id,
            decision_snapshot_id="decision-snapshot",
            decision_snapshot_hash="d" * 64,
            account_id="paper-account",
            broker_environment=BrokerEnvironment.PAPER,
            policy_version="policy-1",
            target_weights={"NEW": 0.1},
            proposed_trades=(),
            turnover_estimate=0,
            tax_estimate=PlanTaxEstimate(currency="USD", estimated_cost=0.0, known=True),
            prompt_versions={"synthesis": "synthesis-v4"},
            model_versions={"synthesis": "model-v2"},
        )
    )
    repository = SqliteOutcomeRepository(database)

    schedule_ids = PlanOutcomeScheduler(
        repository,
        theses,
        snapshots,
        BenchmarkBinding(
            benchmark_id="configured-benchmark",
            provider="point-in-time-market",
            weights={"NEW": 1.0},
        ),
    ).schedule(
        plan,
        (revision.revision_id,),
    )
    schedules = repository.due(as_of=valid_until)

    assert (
        schedule_ids,
        tuple(item.boundary for item in schedules),
        tuple(item.observe_at for item in schedules),
        {
            (
                item.thesis_revision_id,
                item.plan_id,
                item.plan_hash,
                item.benchmark_snapshot_id,
                item.benchmark_provider,
                item.portfolio_input_snapshot_hash,
            )
            for item in schedules
        },
    ) == (
        tuple(item.schedule_id for item in schedules),
        (OutcomeBoundary.EVENT, OutcomeBoundary.REVIEW, OutcomeBoundary.HORIZON),
        (event_at, review_at, valid_until),
        {
            (
                revision.revision_id,
                plan.payload.plan_id,
                plan.plan_hash,
                market.snapshot_id,
                "point-in-time-market",
                portfolio.snapshot_id,
            )
        },
    )
