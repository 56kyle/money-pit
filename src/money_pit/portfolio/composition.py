"""Module composing production A5 planning and A6 read dependencies."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from money_pit.config import ApplicationConfig
from money_pit.config import canonical_config_hash
from money_pit.config import claim_refresh_policy
from money_pit.config import resolve_alpaca_credentials
from money_pit.execution_control.composition import build_execution_gateway_dependencies
from money_pit.execution_control.gateway import ExecutionGatewayDependencies
from money_pit.plans.repository import PortfolioPlanRepository
from money_pit.portfolio.decision_repository import DecisionSnapshotRepository
from money_pit.portfolio.optimizer import ClarabelOptimizer
from money_pit.portfolio.outcome_repository import SqliteOutcomeRepository
from money_pit.portfolio.outcomes import BenchmarkBinding
from money_pit.portfolio.outcomes import PlanOutcomeScheduler
from money_pit.portfolio.persistent_inputs import InstrumentAuthority
from money_pit.portfolio.persistent_inputs import PersistentPortfolioPlanningInputProvider
from money_pit.portfolio.planning import DeterministicPortfolioPlanningService
from money_pit.portfolio.policy import PortfolioPolicy
from money_pit.portfolio.providers import LiquidityStateProvider
from money_pit.portfolio.providers import MarketStateProvider
from money_pit.portfolio.providers import PortfolioStateProvider
from money_pit.portfolio.providers import RiskStateProvider
from money_pit.portfolio.providers import TaxLotStateProvider
from money_pit.portfolio.repository import SnapshotRepository
from money_pit.portfolio.runtime import AlpacaPortfolioStateProvider
from money_pit.portfolio.runtime import ConfiguredTaxLotProvider
from money_pit.portfolio.runtime import DurableEvidenceFreshnessReader
from money_pit.portfolio.runtime import ExactTaxStateValidator
from money_pit.portfolio.runtime import YFinanceLiquidityStateProvider
from money_pit.portfolio.runtime import YFinanceRiskStateProvider
from money_pit.portfolio.runtime import YFinanceStateProvider
from money_pit.portfolio.theses import ThesisRepository
from money_pit.reports.service import StaticPortfolioReportWriter
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.tax import LotSelectionPolicy
from money_pit.storage.database import Database


@dataclass(frozen=True)
class PortfolioRuntime:
    """Concrete A5 service and separately scoped A6 gateway dependencies."""

    planning: DeterministicPortfolioPlanningService
    execution: ExecutionGatewayDependencies | None
    portfolio: PortfolioStateProvider
    market: MarketStateProvider
    tax_lots: TaxLotStateProvider


class PortfolioInstrumentStateProvider(PortfolioStateProvider, InstrumentAuthority, Protocol):
    """Read-only broker state plus authoritative instrument resolution."""


@dataclass(frozen=True)
class PortfolioReadDependencies:
    """Injectable real-behavior seam for A5/A6 read providers."""

    portfolio: PortfolioInstrumentStateProvider
    market: MarketStateProvider
    risk: RiskStateProvider
    liquidity: LiquidityStateProvider
    tax_lots: TaxLotStateProvider


def build_portfolio_runtime(
    *,
    database: Database,
    config: ApplicationConfig,
    reports_root: Path,
    processor_versions: dict[str, str],
    model_versions: dict[str, str],
    prompt_versions: dict[str, str],
    implementation_version: str,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    read_dependencies: PortfolioReadDependencies | None = None,
) -> PortfolioRuntime:
    """Compose GET-only planning reads and the sole lazy capital-write gateway."""
    strategy = config.require_strategy()
    execution_config = config.execution
    if execution_config is not None and execution_config.broker_environment is not strategy.portfolio_environment:
        raise ValueError("portfolio and execution broker environments must match")
    execution_policy = (
        None
        if execution_config is None
        else ExecutionPolicy(
            policy_version=execution_config.policy_version,
            broker_environment=execution_config.broker_environment,
            execution_mode=execution_config.execution_mode,
            allowed_asset_classes=execution_config.allowed_asset_classes,
            maximum_order_notional=execution_config.maximum_order_notional,
            maximum_daily_turnover=execution_config.maximum_daily_turnover,
        )
    )
    if read_dependencies is None:
        credentials = resolve_alpaca_credentials(config.environment, strategy.portfolio_environment)
        production_portfolio = AlpacaPortfolioStateProvider(
            credentials,
            clock=clock,
            instrument_asset_classes=strategy.instrument_asset_classes,
        )
        production_market = YFinanceStateProvider(
            historical_period=strategy.market_history_period,
            maximum_participation_rate=strategy.maximum_daily_volume_participation,
            minimum_average_daily_notional=strategy.minimum_average_daily_notional,
            factor_loadings=strategy.factor_loadings,
            sector_taxonomy=strategy.sector_taxonomy,
        )
        read_dependencies = PortfolioReadDependencies(
            portfolio=production_portfolio,
            market=production_market,
            risk=YFinanceRiskStateProvider(production_market),
            liquidity=YFinanceLiquidityStateProvider(production_market),
            tax_lots=ConfiguredTaxLotProvider(strategy.tax_lot_ledger_path),
        )
    portfolio = read_dependencies.portfolio
    market = read_dependencies.market
    risk = read_dependencies.risk
    liquidity = read_dependencies.liquidity
    tax_lots = read_dependencies.tax_lots
    inputs = PersistentPortfolioPlanningInputProvider(
        database=database,
        strategy=strategy,
        portfolio=portfolio,
        market=market,
        risk=risk,
        liquidity=liquidity,
        tax_lots=tax_lots,
        instruments=portfolio,
        source_config_hash=canonical_config_hash(config.sources),
        strategy_config_hash=canonical_config_hash(strategy),
        execution_config_hash=(canonical_config_hash(execution_config) if execution_config is not None else None),
        execution_policy_hash=(None if execution_policy is None else execution_policy.fingerprint()),
        execution_policy_version=(None if execution_policy is None else execution_policy.policy_version),
        processor_versions=processor_versions,
        model_versions=model_versions,
        prompt_versions=prompt_versions,
        trade_generation_version=implementation_version,
    )
    factor_names = {factor for row in strategy.factor_loadings.values() for factor in row}
    policy = PortfolioPolicy(
        policy_version=strategy.version,
        risk_aversion=strategy.risk_aversion,
        turnover_penalty=strategy.turnover_penalty,
        tax_penalty=strategy.tax_penalty,
        minimum_cash_weight=strategy.cash_minimum,
        maximum_equity_exposure=strategy.maximum_equity_exposure,
        maximum_single_stock_exposure=strategy.maximum_single_stock_exposure,
        maximum_thematic_etf_exposure=strategy.maximum_thematic_etf_exposure,
        maximum_turnover=strategy.turnover_limit,
        minimum_trade_weight=0.0,
        maximum_position_change=strategy.position_change_limit,
        maximum_sector_weights=dict.fromkeys(set(strategy.sector_taxonomy.values()), strategy.sector_weight_limit),
        maximum_factor_exposures=dict.fromkeys(factor_names, strategy.factor_weight_limit),
        maximum_correlated_group_weights=dict.fromkeys(
            strategy.correlated_exposure_groups,
            strategy.correlated_exposure_limit,
        ),
        accept_optimal_inaccurate=strategy.accept_optimal_inaccurate,
        feasibility_tolerance=strategy.feasibility_tolerance,
    )
    snapshots = SnapshotRepository(database)
    planning = DeterministicPortfolioPlanningService(
        inputs=inputs,
        optimizer=ClarabelOptimizer(),
        policy=policy,
        snapshots=snapshots,
        decisions=DecisionSnapshotRepository(database),
        plans=PortfolioPlanRepository(database),
        reports=StaticPortfolioReportWriter(reports_root, reports_root.parent / "assets"),
        outcomes=PlanOutcomeScheduler(
            SqliteOutcomeRepository(database),
            ThesisRepository(database),
            snapshots,
            BenchmarkBinding(
                benchmark_id=strategy.benchmark_id,
                provider=strategy.benchmark_provider,
                weights=strategy.benchmark_weights,
            ),
        ),
        plan_ttl=timedelta(seconds=strategy.plan_ttl_seconds),
        implementation_version=implementation_version,
        tax_lot_policy=LotSelectionPolicy(strategy.tax_lot_policy),
        minimum_trade_notional=strategy.minimum_trade_notional,
        maximum_slippage_bps=strategy.maximum_slippage_fraction * 10_000,
        specific_tax_lot_ids=strategy.specific_tax_lot_ids,
        clock=clock,
    )
    execution: ExecutionGatewayDependencies | None = None
    if execution_config is not None:
        execution = build_execution_gateway_dependencies(
            database=database,
            secrets=config.environment,
            broker_environment=execution_config.broker_environment,
            execution_config_hash=canonical_config_hash(execution_config),
            portfolio=portfolio,
            market=market,
            tax_lots=tax_lots,
            evidence=DurableEvidenceFreshnessReader(
                database,
                refresh_policy=claim_refresh_policy(config.intelligence),
            ),
            tax_validator=ExactTaxStateValidator(),
            maximum_market_drift_fraction=execution_config.maximum_market_drift_fraction,
            maximum_quote_age_seconds=execution_config.maximum_quote_age_seconds,
            poll_interval_seconds=execution_config.poll_interval_seconds,
            poll_timeout_seconds=execution_config.poll_timeout_seconds,
            clock=clock,
        )
    return PortfolioRuntime(
        planning=planning,
        execution=execution,
        portfolio=portfolio,
        market=market,
        tax_lots=tax_lots,
    )
