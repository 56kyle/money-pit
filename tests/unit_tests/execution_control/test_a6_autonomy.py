"""Tests for staged autonomous-execution qualification."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from money_pit.execution_control.autonomy import AutonomousActionCoverage
from money_pit.execution_control.autonomy import AutonomousExecutionPolicy
from money_pit.execution_control.autonomy import AutonomyReadinessEvidence
from money_pit.execution_control.autonomy import StagedAutonomyEvaluator
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.portfolio_plan import PlanTaxEstimate
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade


_NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _plan() -> PortfolioPlan:
    return PortfolioPlan.from_payload(
        PortfolioPlanPayload(
            plan_id="autonomy-plan",
            created_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
            portfolio_snapshot_id="portfolio-1",
            market_snapshot_id="market-1",
            decision_snapshot_id="decision-1",
            decision_snapshot_hash="2" * 64,
            account_id="paper-account",
            broker_environment=BrokerEnvironment.PAPER,
            policy_version="policy-2",
            target_weights={"SPY": 0.5},
            proposed_trades=(
                ProposedTrade(
                    instrument="SPY",
                    asset_class=TradableAssetClass.US_ETF,
                    side="buy",
                    quantity=1,
                    estimated_notional=600,
                    tax_cost_known=True,
                ),
            ),
            turnover_estimate=0.05,
            tax_estimate=PlanTaxEstimate(currency="USD", estimated_cost=0.0, known=True),
            evidence_gate_results={"verified": True},
            constraint_results={"bounded": True},
        )
    )


def _execution_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        policy_version="policy-2",
        broker_environment=BrokerEnvironment.LIVE,
        execution_mode=ExecutionMode.AUTONOMOUS,
        maximum_order_notional=1_000,
        maximum_daily_turnover=0.1,
    )


def _evaluator(
    *,
    readiness: AutonomyReadinessEvidence | None = None,
    policy: AutonomousExecutionPolicy | None = None,
    coverage: AutonomousActionCoverage | None = None,
    durable: bool = True,
) -> StagedAutonomyEvaluator:
    return StagedAutonomyEvaluator(
        readiness=readiness
        or AutonomyReadinessEvidence(
            as_of=_NOW,
            validation_period_start=_NOW - timedelta(days=120),
            evidence_event_ids=("qualification-1",),
            shadow_trading_days=60,
            executable_shadow_plans=30,
            approved_paper_executions=30,
            approval_required_live_executions=30,
            critical_control_failures=0,
        ),
        autonomous_policy=policy
        or AutonomousExecutionPolicy(
            policy_version="policy-2",
            enabled=True,
            maximum_order_notional=900,
            maximum_daily_turnover=0.08,
        ),
        coverage=coverage
        or AutonomousActionCoverage(asset_classes={"SPY": "us_equity"}, liquid_instruments=frozenset({"SPY"})),
        verify_durable_evidence=lambda _evidence: durable,
    )


def test_denials_accepts_exact_staged_thresholds_and_bounded_scope() -> None:
    assert _evaluator().denials(_plan(), _execution_policy()) == ()


def test_denials_rejects_readiness_without_durable_records() -> None:
    denials = _evaluator(durable=False).denials(_plan(), _execution_policy())

    assert any("not backed by durable" in denial.detail for denial in denials)


@pytest.mark.parametrize(
    ("readiness_update", "expected_fragment"),
    [
        ({"shadow_trading_days": 59}, "60 shadow trading days"),
        ({"executable_shadow_plans": 29}, "30 executable shadow plans"),
        ({"approved_paper_executions": 29}, "30 approved paper executions"),
        ({"approval_required_live_executions": 29}, "30 approval-required live executions"),
        ({"critical_control_failures": 1}, "zero critical control failures"),
    ],
)
def test_denials_rejects_incomplete_staged_evidence(
    readiness_update: dict[str, int],
    expected_fragment: str,
) -> None:
    readiness = AutonomyReadinessEvidence(
        as_of=_NOW,
        validation_period_start=_NOW - timedelta(days=120),
        evidence_event_ids=("qualification-1",),
        shadow_trading_days=60,
        executable_shadow_plans=30,
        approved_paper_executions=30,
        approval_required_live_executions=30,
        critical_control_failures=0,
    ).model_copy(update=readiness_update)

    denials = _evaluator(readiness=readiness).denials(_plan(), _execution_policy())

    assert any(expected_fragment in denial.detail for denial in denials)


@pytest.mark.parametrize(
    ("autonomous_policy", "coverage", "expected_fragment"),
    [
        (
            AutonomousExecutionPolicy(
                policy_version="policy-2",
                enabled=False,
                maximum_order_notional=900,
                maximum_daily_turnover=0.08,
            ),
            None,
            "not explicitly enabled",
        ),
        (
            AutonomousExecutionPolicy(
                policy_version="old",
                enabled=True,
                maximum_order_notional=900,
                maximum_daily_turnover=0.08,
            ),
            None,
            "version differs",
        ),
        (
            None,
            AutonomousActionCoverage(asset_classes={"SPY": "crypto"}, liquid_instruments=frozenset({"SPY"})),
            "does not cover asset classes",
        ),
        (
            None,
            AutonomousActionCoverage(asset_classes={"SPY": "us_equity"}, liquid_instruments=frozenset()),
            "liquidity coverage is absent",
        ),
        (
            None,
            AutonomousActionCoverage(
                asset_classes={"SPY": "us_equity"},
                liquid_instruments=frozenset({"SPY"}),
                short_opening_instruments=frozenset({"SPY"}),
            ),
            "cannot open short exposure",
        ),
    ],
)
def test_denials_rejects_uncovered_autonomous_policy_or_scope(
    autonomous_policy: AutonomousExecutionPolicy | None,
    coverage: AutonomousActionCoverage | None,
    expected_fragment: str,
) -> None:
    denials = _evaluator(policy=autonomous_policy, coverage=coverage).denials(_plan(), _execution_policy())

    assert any(expected_fragment in denial.detail for denial in denials)
