"""Module composing the production persistent A1-A6 application."""

import sqlite3  # noqa: TC003 - sqlite Row is required by runtime validation.
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from money_pit.agents.discovery import make_discovery_agent
from money_pit.agents.interpretation import make_interpretation_agent
from money_pit.agents.research_planner import make_research_planning_agent
from money_pit.agents.synthesis import make_synthesis_agent
from money_pit.claims.repository import ClaimRepository
from money_pit.config import ApplicationConfig
from money_pit.config import canonical_config_hash
from money_pit.config import claim_refresh_policy
from money_pit.contracts import DiscoveryAgent
from money_pit.contracts import InterpretationAgent
from money_pit.contracts import ResearchPlanningAgent
from money_pit.contracts import SynthesisAgent
from money_pit.evidence.work import EvidenceWorkStore
from money_pit.execution_control.gateway import execute_plan
from money_pit.execution_control.node import make_execution_node
from money_pit.graph.state import PipelineState
from money_pit.pipeline.artifacts import reconcile_stage_artifact_files
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.discovery import make_discovery_node
from money_pit.pipeline.interpretation import InterpretationService
from money_pit.pipeline.interpretation import make_interpretation_node
from money_pit.pipeline.orchestration import HarnessNodes
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.pipeline.research import ResearchBudget
from money_pit.pipeline.research import make_research_node
from money_pit.pipeline.synthesis import make_synthesis_node
from money_pit.portfolio.composition import PortfolioRuntime
from money_pit.portfolio.composition import build_portfolio_runtime
from money_pit.portfolio.node import make_portfolio_planning_node
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.portfolio.theses import ThesisRepository
from money_pit.portfolio.universe import ConfiguredInstrumentResolver
from money_pit.portfolio.universe import InstrumentResolver
from money_pit.portfolio.universe import LayeredUniverse
from money_pit.portfolio.universe import build_layered_universe
from money_pit.research.composition import build_research_runtime
from money_pit.research.providers import BraveResearchProvider
from money_pit.research.providers import BraveSearchBackend
from money_pit.research.providers import EdgarResearchProvider
from money_pit.research.providers import EdgarSearchBackend
from money_pit.research.providers import FredResearchProvider
from money_pit.research.registry import ResearchProviderRegistry
from money_pit.runs.manifest import register_run
from money_pit.runs.paths import RepositoryPaths
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.runs import RunFailureDetail
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import RunTerminalEvent
from money_pit.schemas.runs import RunTerminalStatus
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.universe import UniverseLayer
from money_pit.sources.http import AddressPinnedHttpTransport
from money_pit.storage.admission import IntelligenceAdmissionRepository
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.runs import RunRepository


@dataclass(frozen=True)
class ApplicationRuntime:
    """Capability-scoped nodes and durable run store."""

    nodes: HarnessNodes
    runs: RunRepository


@dataclass(frozen=True)
class ApplicationDependencies:
    """Injected model, research, and optional capital-stage capabilities."""

    interpretation_agent: InterpretationAgent
    discovery_agent: DiscoveryAgent
    research_planning_agent: ResearchPlanningAgent
    synthesis_agent: SynthesisAgent
    research_providers: ResearchProviderRegistry
    instrument_resolver: InstrumentResolver
    clock: Callable[[], datetime]
    run_id_factory: Callable[[], str]
    portfolio_runtime_factory: Callable[[], PortfolioRuntime] | None = None


class ApplicationDependencyError(Exception):
    """Raised when a requested stage lacks an explicitly composed capability."""


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _uuid4_string() -> str:
    return str(uuid.uuid4())


def execute_harness_run(
    *,
    database: Database,
    config: ApplicationConfig,
    paths: RepositoryPaths,
    source_id: str | None,
    requested_as_of: datetime | None,
    through: Stage,
    implementation_version: str,
    dependencies: ApplicationDependencies | None = None,
) -> PipelineState:
    """Create one immutable run manifest and invoke the configured staged harness."""
    paths.ensure_writable_roots()
    resolved_dependencies = dependencies or build_production_application_dependencies(
        database=database,
        config=config,
        reports_root=paths.reports_root,
        implementation_version=implementation_version,
        through=through,
    )
    started_at = resolved_dependencies.clock()
    cutoff = requested_as_of or started_at
    run_record = RunRecord(
        run_id=resolved_dependencies.run_id_factory(),
        requested_as_of=cutoff,
        started_at=started_at,
        known_at=started_at,
        through_stage=through.value,
        source_config_hash=canonical_config_hash(config.sources),
        intelligence_config_hash=(None if through is Stage.A1 else canonical_config_hash(config.intelligence)),
        portfolio_config_hash=(
            canonical_config_hash(config.require_strategy()) if through in {Stage.A5, Stage.A6} else None
        ),
        execution_config_hash=(canonical_config_hash(config.require_execution()) if through is Stage.A6 else None),
    )
    runtime = build_application_runtime(
        database=database,
        config=config,
        assets_root=paths.assets_root,
        implementation_version=implementation_version,
        through=through,
        dependencies=resolved_dependencies,
    )
    run_store = RunRepository(database)
    run_dir = register_run(paths, run_store, run_record)
    try:
        result = run_pipeline(
            initial_state(
                run_id=run_record.run_id,
                run_dir=run_dir,
                requested_as_of=cutoff,
                run_started_at=started_at,
                requested_as_of_explicit=requested_as_of is not None,
                config=config,
                through=through,
                source_id=source_id,
            ),
            nodes=runtime.nodes,
            through=through,
            run_record=run_record,
        )
        reconcile_stage_artifact_files(
            run_dir,
            run_store.artifacts_for_run(run_record.run_id),
        )
    except Exception as error:
        failed_at = datetime.now(tz=UTC)
        run_store.append_terminal_event(
            RunTerminalEvent(
                run_id=run_record.run_id,
                status=RunTerminalStatus.FAILED,
                completed_at=failed_at,
                known_at=failed_at,
                failure_kind=type(error).__name__[:128],
                failure_detail=RunFailureDetail(
                    durable_record_ids=(),
                    retryable=False,
                ),
            )
        )
        raise
    completed_at = datetime.now(tz=UTC)
    run_store.append_terminal_event(
        RunTerminalEvent(
            run_id=run_record.run_id,
            status=RunTerminalStatus.COMPLETED,
            completed_at=completed_at,
            known_at=completed_at,
        )
    )
    return result


def build_application_runtime(
    *,
    database: Database,
    config: ApplicationConfig,
    assets_root: Path,
    implementation_version: str,
    through: Stage,
    dependencies: ApplicationDependencies,
) -> ApplicationRuntime:
    """Compose all stages while exposing broker writes only to the A6 closure."""
    claims = ClaimRepository(database, refresh_policy=claim_refresh_policy(config.intelligence))
    theses = ThesisRepository(database)
    run_store = RunRepository(database)
    interpretation_agent = dependencies.interpretation_agent
    evidence_work = EvidenceWorkStore(database)
    admission = IntelligenceAdmissionRepository(database)
    interpreter = InterpretationService(
        evidence=evidence_work,
        admission=admission,
        agent=interpretation_agent,
        implementation_version=implementation_version,
        clock=dependencies.clock,
    )
    providers = dependencies.research_providers
    research = build_research_runtime(
        database,
        providers,
        AssetStore(assets_root),
        claims=claims,
        interpreter=interpreter,
        clock=dependencies.clock,
    )
    portfolio: PortfolioRuntime | None = None
    if through in {Stage.A5, Stage.A6}:
        if dependencies.portfolio_runtime_factory is None:
            raise ApplicationDependencyError(f"{through.value} requires portfolio runtime capabilities")
        portfolio = dependencies.portfolio_runtime_factory()
    artifact_store = run_store
    load_universe = _universe_loader(config, claims, database, dependencies.instrument_resolver)
    execution_policy = _execution_policy(config) if through is Stage.A6 else None
    execution_dependencies = None if portfolio is None else portfolio.execution
    if through is Stage.A6 and execution_dependencies is None:
        raise ApplicationDependencyError("A6 requires execution gateway capabilities")
    nodes = HarnessNodes(
        a1=make_interpretation_node(
            evidence=research.evidence_work,
            admission=admission,
            agent=interpretation_agent,
            implementation_version=implementation_version,
            interpretation_service=interpreter,
            clock=dependencies.clock,
        ),
        a2=make_discovery_node(
            claims=claims,
            theses=theses,
            research_tasks=research.planned_tasks,
            load_universe=load_universe,
            agent=dependencies.discovery_agent,
            implementation_version=implementation_version,
            artifact_store=artifact_store,
            allowed_provider_names=providers.names(),
            clock=dependencies.clock,
        ),
        a3=make_research_node(
            theses=theses,
            tasks=research.planned_tasks,
            runner=research.runner,
            planner=dependencies.research_planning_agent,
            implementation_version=implementation_version,
            admission=admission,
            allowed_provider_names=providers.names(),
            clock=dependencies.clock,
            budget=ResearchBudget(
                maximum_rounds=config.intelligence.research_budget.maximum_rounds,
                maximum_queries=config.intelligence.research_budget.maximum_queries,
                maximum_fetches=config.intelligence.research_budget.maximum_fetches,
                maximum_elapsed=timedelta(seconds=config.intelligence.research_budget.maximum_elapsed_seconds),
            ),
        ),
        a4=make_synthesis_node(
            claims=claims,
            theses=theses,
            agent=dependencies.synthesis_agent,
            resolver_version=implementation_version,
            verifier_version=implementation_version,
            synthesis_version=implementation_version,
            admission=admission,
            proxy_relationships={
                key: frozenset({value}) for key, value in config.intelligence.explicit_proxies.items()
            },
            clock=dependencies.clock,
        ),
        a5=None
        if portfolio is None
        else make_portfolio_planning_node(
            service=portfolio.planning,
            implementation_version=implementation_version,
            artifact_store=artifact_store,
            clock=dependencies.clock,
        ),
        a6=None
        if execution_dependencies is None or execution_policy is None
        else make_execution_node(
            execute=lambda plan_id: execute_plan(plan_id, execution_policy, execution_dependencies),
            artifact_store=artifact_store,
            implementation_version=implementation_version,
            clock=dependencies.clock,
        ),
    )
    return ApplicationRuntime(nodes=nodes, runs=run_store)


def build_production_application_dependencies(
    *,
    database: Database,
    config: ApplicationConfig,
    reports_root: Path,
    implementation_version: str,
    through: Stage,
) -> ApplicationDependencies:
    """Resolve production capabilities at the outer application boundary."""
    clock: Callable[[], datetime] = _utc_now
    portfolio_factory: Callable[[], PortfolioRuntime] | None = None
    if through in {Stage.A5, Stage.A6}:

        def build_capital_runtime() -> PortfolioRuntime:
            return build_portfolio_runtime(
                database=database,
                config=config,
                reports_root=reports_root,
                processor_versions={"builtin_evidence_processors": implementation_version},
                model_versions={"llm": config.environment.llm_model},
                prompt_versions={
                    "interpretation": implementation_version,
                    "discovery": implementation_version,
                    "research": implementation_version,
                    "synthesis": implementation_version,
                },
                implementation_version=implementation_version,
                clock=clock,
            )

        portfolio_factory = build_capital_runtime
    return ApplicationDependencies(
        interpretation_agent=make_interpretation_agent(config.environment),
        discovery_agent=make_discovery_agent(config.environment),
        research_planning_agent=make_research_planning_agent(config.environment),
        synthesis_agent=make_synthesis_agent(config.environment),
        research_providers=build_configured_research_registry(config, clock=clock),
        instrument_resolver=_configured_instrument_resolver(config),
        clock=clock,
        run_id_factory=_uuid4_string,
        portfolio_runtime_factory=portfolio_factory,
    )


def build_configured_research_registry(
    config: ApplicationConfig,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> ResearchProviderRegistry:
    """Build providers only when both source policy and credentials are configured."""
    registry = ResearchProviderRegistry()
    transport = AddressPinnedHttpTransport()
    brave_definition = _research_source_definition(config, "brave")
    if brave_definition is not None:
        api_key = config.environment.brave_search_api_key
        if api_key is None:
            raise ApplicationDependencyError("Configured Brave research requires MONEY_PIT__BRAVE_SEARCH_API_KEY")
        backend = BraveSearchBackend(api_key.get_secret_value(), transport)
        registry.register(
            BraveResearchProvider(
                brave_definition,
                backend,
                transport,
                publisher_policies=_research_publisher_definitions(config, "brave"),
                clock=clock,
            ),
        )
    edgar_definition = _research_source_definition(config, "edgar")
    if edgar_definition is not None:
        configured_identity = config.environment.sec_user_agent
        if configured_identity is None:
            raise ApplicationDependencyError("Configured EDGAR research requires MONEY_PIT__SEC_USER_AGENT")
        user_agent = configured_identity.get_secret_value()
        backend = EdgarSearchBackend(user_agent, transport)
        registry.register(
            EdgarResearchProvider(
                edgar_definition,
                backend,
                transport,
                user_agent=user_agent,
                clock=clock,
            ),
        )
    fred_definition = _research_source_definition(config, "fred")
    if fred_definition is not None:
        api_key = config.environment.fred_api_key
        if api_key is None:
            raise ApplicationDependencyError("Configured FRED research requires MONEY_PIT__FRED_API_KEY")
        registry.register(
            FredResearchProvider(
                fred_definition,
                transport,
                api_key=api_key.get_secret_value(),
                clock=clock,
            ),
        )
    return registry


def _research_source_definition(
    config: ApplicationConfig,
    provider_name: str,
) -> SourceDefinition | None:
    """Return the unique enabled source policy tagged for a provider."""
    required_tags = frozenset({"research-provider", f"provider:{provider_name}"})
    matches = tuple(
        source for source in config.sources.sources if source.enabled and required_tags.issubset(source.tags)
    )
    if len(matches) > 1:
        raise ApplicationDependencyError(f"Research provider {provider_name!r} has multiple source policies")
    return matches[0] if matches else None


def _research_publisher_definitions(
    config: ApplicationConfig,
    provider_name: str,
) -> tuple[SourceDefinition, ...]:
    """Return configured destination policies for a web-search provider."""
    required_tags = frozenset({"research-publisher", f"provider:{provider_name}"})
    matches = tuple(
        source for source in config.sources.sources if source.enabled and required_tags.issubset(source.tags)
    )
    if not matches:
        raise ApplicationDependencyError(
            f"Research provider {provider_name!r} requires at least one publisher policy",
        )
    return matches


def _configured_instrument_resolver(config: ApplicationConfig) -> InstrumentResolver:
    intelligence = config.intelligence
    strategy = config.strategy
    approved = {
        *({} if strategy is None else strategy.strategic_core_targets),
        *intelligence.watchlist,
        *intelligence.benchmark_constituents,
        *intelligence.explicit_proxies.values(),
        *(instrument for screen in intelligence.quantitative_screens for instrument in screen.instruments),
    }
    return ConfiguredInstrumentResolver(frozenset(approved))


def initial_state(
    *,
    run_id: str,
    run_dir: Path,
    requested_as_of: datetime,
    run_started_at: datetime,
    requested_as_of_explicit: bool,
    config: ApplicationConfig,
    through: Stage,
    source_id: str | None,
) -> PipelineState:
    """Build the exact initial harness state and configuration bindings."""
    state = PipelineState(
        run_id=run_id,
        run_dir=str(run_dir),
        requested_as_of=requested_as_of,
        run_started_at=run_started_at,
        requested_as_of_explicit=requested_as_of_explicit,
        source_config_hash=canonical_config_hash(config.sources),
        source_id=source_id,
        completed_stages=(),
        artifact_ids=(),
    )
    if through is not Stage.A1:
        state["intelligence_config_hash"] = canonical_config_hash(config.intelligence)
    if through in {Stage.A5, Stage.A6}:
        state["portfolio_config_hash"] = canonical_config_hash(config.require_strategy())
    if through is Stage.A6:
        state["execution_config_hash"] = canonical_config_hash(config.require_execution())
    return state


def _execution_policy(config: ApplicationConfig) -> ExecutionPolicy:
    execution = config.require_execution()
    return ExecutionPolicy(
        policy_version=execution.policy_version,
        broker_environment=execution.broker_environment,
        execution_mode=execution.execution_mode,
        allowed_asset_classes=execution.allowed_asset_classes,
        maximum_order_notional=execution.maximum_order_notional,
        maximum_daily_turnover=execution.maximum_daily_turnover,
    )


def _universe_loader(
    config: ApplicationConfig,
    claims: ClaimRepository,
    database: Database,
    resolver: InstrumentResolver,
) -> Callable[[datetime], LayeredUniverse]:
    def load(as_of: datetime) -> LayeredUniverse:
        portfolio = _portfolio_snapshot_as_of(database, as_of)
        held: set[str] = set() if portfolio is None else {item.instrument for item in portfolio.payload.positions}

        source_mentions = {
            instrument
            for observation in claims.observations_as_of(as_of=as_of)
            for instrument in observation.instruments
        }
        return build_layered_universe(
            holdings=(resolver.resolve(item, layer=UniverseLayer.HOLDING) for item in held),
            watchlist=(resolver.resolve(item, layer=UniverseLayer.WATCHLIST) for item in config.intelligence.watchlist),
            source_mentions=(resolver.resolve(item, layer=UniverseLayer.SOURCE_MENTION) for item in source_mentions),
            benchmark_constituents=(
                resolver.resolve(item, layer=UniverseLayer.BENCHMARK)
                for item in config.intelligence.benchmark_constituents
            ),
            quantitative_screens=(
                resolver.resolve(instrument, layer=UniverseLayer.QUANTITATIVE_SCREEN)
                for screen in config.intelligence.quantitative_screens
                for instrument in screen.instruments
            ),
            explicit_proxies=(
                resolver.resolve(instrument, layer=UniverseLayer.EXPLICIT_PROXY, proxy_for=reference)
                for reference, instrument in config.intelligence.explicit_proxies.items()
            ),
            portfolio_gaps=(
                resolver.resolve(instrument, layer=UniverseLayer.PORTFOLIO_GAP)
                for instrument in (() if config.strategy is None else config.strategy.strategic_core_targets)
                if instrument not in held
            ),
        )

    return load


def _portfolio_snapshot_as_of(database: Database, as_of: datetime) -> PortfolioStateSnapshot | None:
    with database.transaction() as connection:
        row: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                "SELECT payload_json FROM portfolio_state_snapshots WHERE captured_at <= ? ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1",
                (as_of.isoformat(),),
            ).fetchone(),
        )
    if row is None:
        return None
    try:
        return PortfolioStateSnapshot.model_validate_json(str(cast("object", row["payload_json"])))
    except (ValueError, ValidationError) as error:
        raise ValueError("durable portfolio snapshot is malformed") from error
