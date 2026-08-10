"""Module containing production composition for the scoped A6 gateway."""

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from money_pit.alpaca_orders import FillObserver
from money_pit.alpaca_orders import make_alpaca_fill_observer
from money_pit.alpaca_orders import make_alpaca_order_placer_factory
from money_pit.execution_control.gateway import ExecutionGatewayDependencies
from money_pit.execution_control.protocols import AutonomousEligibilityEvaluator
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.execution_control.repository import TurnoverReservationRepository
from money_pit.execution_control.state_checks import EvidenceFreshnessReader
from money_pit.execution_control.state_checks import ProductionStateCheckFactory
from money_pit.execution_control.state_checks import TaxStateValidator
from money_pit.plans.repository import PortfolioPlanRepository
from money_pit.portfolio.decision_repository import DecisionSnapshotRepository
from money_pit.portfolio.providers import MarketStateProvider
from money_pit.portfolio.providers import PortfolioStateProvider
from money_pit.portfolio.providers import TaxLotStateProvider
from money_pit.portfolio.repository import SnapshotRepository
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.secrets import ExecutionCredentialResolver
from money_pit.storage.database import Database


def _lazy_fill_observer(credentials: ExecutionCredentialResolver, environment: BrokerEnvironment) -> FillObserver:
    """Resolve credentials and construct a read client only on the first A6 observation."""
    observer: FillObserver | None = None

    def observe(client_order_id: str):
        nonlocal observer
        if observer is None:
            observer = make_alpaca_fill_observer(
                credentials.alpaca_execution(environment, reason="Observe fills for an exact approved plan")
            )
        return observer(client_order_id)

    return observe


def build_execution_gateway_dependencies(
    *,
    database: Database,
    credentials: ExecutionCredentialResolver,
    broker_environment: BrokerEnvironment,
    execution_config_hash: str,
    portfolio: PortfolioStateProvider,
    market: MarketStateProvider,
    tax_lots: TaxLotStateProvider,
    evidence: EvidenceFreshnessReader,
    tax_validator: TaxStateValidator,
    maximum_market_drift_fraction: float,
    maximum_quote_age_seconds: float,
    poll_interval_seconds: float,
    poll_timeout_seconds: float,
    autonomous_eligibility: AutonomousEligibilityEvaluator | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> ExecutionGatewayDependencies:
    """Compose real read dependencies while keeping the broker writer lazy until A6 preflight passes."""
    authority = SqliteExecutionAuthorityRepository(database)
    state_checks = ProductionStateCheckFactory(
        decisions=DecisionSnapshotRepository(database),
        snapshots=SnapshotRepository(database),
        portfolio=portfolio,
        market=market,
        tax_lots=tax_lots,
        evidence=evidence,
        tax_validator=tax_validator,
        maximum_market_drift_fraction=maximum_market_drift_fraction,
        maximum_quote_age=timedelta(seconds=maximum_quote_age_seconds),
        clock=clock,
    ).build()
    return ExecutionGatewayDependencies(
        plans=PortfolioPlanRepository(database),
        decisions=authority,
        kill_switch=authority,
        claims=authority,
        journal=authority,
        current_state=state_checks,
        execution_config_hash=execution_config_hash,
        committed_turnover_excluding_plan=authority.committed_turnover_excluding_plan,
        turnover_reservations=TurnoverReservationRepository(database),
        order_placer_factory=make_alpaca_order_placer_factory(
            lambda environment: credentials.alpaca_execution(
                environment,
                reason="Submit orders for an exact approved plan",
            )
        ),
        observe_fill=_lazy_fill_observer(credentials, broker_environment),
        clock=clock,
        autonomous_eligibility=autonomous_eligibility,
        poll_interval_seconds=poll_interval_seconds,
        poll_timeout_seconds=poll_timeout_seconds,
    )
