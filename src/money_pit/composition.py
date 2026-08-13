"""Module composing the production persistent A1-A6 application."""

import hashlib
import json
import sqlite3  # noqa: TC003 - sqlite Row is required by runtime validation.
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import cast

from pydantic import JsonValue
from pydantic import TypeAdapter
from pydantic import ValidationError

from money_pit.agents.discovery import make_discovery_agent
from money_pit.agents.inference import InferenceTracking
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
from money_pit.pipeline.identity import interpretation_policy_version
from money_pit.pipeline.interpretation import InterpretationService
from money_pit.pipeline.interpretation import make_interpretation_node
from money_pit.pipeline.orchestration import HarnessNodes
from money_pit.pipeline.orchestration import IntelligenceNodes
from money_pit.pipeline.orchestration import IntelligenceStage
from money_pit.pipeline.orchestration import IntelligenceUpdateReport
from money_pit.pipeline.orchestration import run_intelligence_update
from money_pit.pipeline.research import ResearchBudget
from money_pit.pipeline.research import make_research_node
from money_pit.pipeline.synthesis import make_synthesis_node
from money_pit.portfolio.composition import PortfolioRuntime
from money_pit.portfolio.composition import build_portfolio_runtime
from money_pit.portfolio.node import make_portfolio_planning_node
from money_pit.portfolio.planning import PortfolioReviewRequest
from money_pit.portfolio.planning import PortfolioReviewResult
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
from money_pit.secrets import ExecutionCredentialResolver
from money_pit.secrets import InferenceCredentialResolver
from money_pit.secrets import OpenAICredentials
from money_pit.secrets import PortfolioCredentialResolver
from money_pit.secrets import ResearchCredentialResolver
from money_pit.secrets import SecretSpecInferenceResolver
from money_pit.secrets import SecretSpecPortfolioResolver
from money_pit.secrets import SecretSpecResearchResolver
from money_pit.sources.http import AddressPinnedHttpTransport
from money_pit.storage.admission import IntelligenceAdmissionRepository
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.intelligence_work import DiscoveryOriginRecord
from money_pit.storage.intelligence_work import DiscoveryUnitKind
from money_pit.storage.intelligence_work import DiscoveryUnitRecord
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import IntelligenceWorkStatus
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
    research_providers: ResearchProviderRegistry
    instrument_resolver: InstrumentResolver
    clock: Callable[[], datetime]
    run_id_factory: Callable[[], str]
    inference_credentials: InferenceCredentialResolver
    discovery_agent: DiscoveryAgent | None = None
    research_planning_agent: ResearchPlanningAgent | None = None
    synthesis_agent: SynthesisAgent | None = None
    portfolio_runtime_factory: Callable[[], PortfolioRuntime] | None = None
    inference_tracking: InferenceTracking | None = None
    intelligence_work: IntelligenceWorkRepository | None = None


class ApplicationDependencyError(Exception):
    """Raised when a requested stage lacks an explicitly composed capability."""


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _uuid4_string() -> str:
    return str(uuid.uuid4())


def execute_intelligence_update(
    *,
    database: Database,
    config: ApplicationConfig,
    paths: RepositoryPaths,
    source_id: str | None,
    requested_as_of: datetime | None,
    through: IntelligenceStage,
    implementation_version: str,
    dependencies: ApplicationDependencies | None = None,
) -> IntelligenceUpdateReport:
    """Run one bounded A1-A4 update with durable lifecycle and usage reporting."""
    paths.ensure_writable_roots()
    pipeline_through = through.pipeline_stage()
    resolved_dependencies = dependencies or build_production_application_dependencies(
        database=database,
        config=config,
        reports_root=paths.reports_root,
        implementation_version=implementation_version,
        through=pipeline_through,
        inference_credentials=SecretSpecInferenceResolver.from_environment(),
        research_credentials=(
            SecretSpecResearchResolver.from_environment() if pipeline_through in {Stage.A3, Stage.A4} else None
        ),
    )
    started_at = resolved_dependencies.clock()
    cutoff = requested_as_of or started_at
    run_record = RunRecord(
        run_id=resolved_dependencies.run_id_factory(),
        requested_as_of=cutoff,
        started_at=started_at,
        known_at=started_at,
        through_stage=pipeline_through.value,
        source_config_hash=canonical_config_hash(config.sources),
        intelligence_config_hash=(None if pipeline_through is Stage.A1 else canonical_config_hash(config.intelligence)),
        portfolio_config_hash=None,
        execution_config_hash=None,
    )
    runtime = build_application_runtime(
        database=database,
        config=config,
        assets_root=paths.assets_root,
        implementation_version=implementation_version,
        through=pipeline_through,
        dependencies=resolved_dependencies,
    )
    nodes = IntelligenceNodes(
        a1=runtime.nodes.a1,
        a2=runtime.nodes.a2,
        a3=runtime.nodes.a3,
        a4=runtime.nodes.a4,
    )
    run_store = RunRepository(database)
    run_dir = register_run(paths, run_store, run_record)
    work = IntelligenceWorkRepository(database)
    try:
        _reconcile_universe_discovery_units(config, work=work, created_at=started_at)
        _reconcile_holding_discovery_units(database, config=config, work=work, created_at=started_at)
        report = run_intelligence_update(
            initial_state(
                run_id=run_record.run_id,
                run_dir=run_dir,
                requested_as_of=cutoff,
                run_started_at=started_at,
                requested_as_of_explicit=requested_as_of is not None,
                config=config,
                through=pipeline_through,
                source_id=source_id,
            ),
            nodes=nodes,
            through=through,
            run_record=run_record,
            work=work,
        )
        report = report.model_copy(
            update={
                "remaining": intelligence_work_status(
                    database=database,
                    config=config,
                    source_id=source_id,
                    implementation_version=implementation_version,
                    as_of=cutoff,
                )
            }
        )
        reconcile_stage_artifact_files(run_dir, run_store.artifacts_for_run(run_record.run_id))
    except Exception as error:
        failed_at = resolved_dependencies.clock()
        run_store.append_terminal_event(
            RunTerminalEvent(
                run_id=run_record.run_id,
                status=RunTerminalStatus.FAILED,
                completed_at=failed_at,
                known_at=failed_at,
                failure_kind=type(error).__name__[:128],
                failure_detail=RunFailureDetail(durable_record_ids=(), retryable=False),
            )
        )
        raise
    completed_at = resolved_dependencies.clock()
    run_store.append_terminal_event(
        RunTerminalEvent(
            run_id=run_record.run_id,
            status=RunTerminalStatus.COMPLETED,
            completed_at=completed_at,
            known_at=completed_at,
        )
    )
    return report


def _reconcile_universe_discovery_units(
    config: ApplicationConfig,
    *,
    work: IntelligenceWorkRepository,
    created_at: datetime,
) -> None:
    """Queue each versioned configured universe entry exactly once per fingerprint."""
    for unit, origins in _configured_universe_discovery_units(config, created_at=created_at):
        work.append_discovery_unit(unit, origins)


def intelligence_work_status(
    *,
    database: Database,
    config: ApplicationConfig,
    source_id: str | None,
    implementation_version: str,
    as_of: datetime | None = None,
) -> IntelligenceWorkStatus:
    """Project provider-free backlog, including work not yet materialized."""
    boundary = as_of or _utc_now()
    interpreter_version = interpretation_policy_version(
        prompt_version=implementation_version,
        model=config.intelligence.llm_model,
    )
    work = IntelligenceWorkRepository(database)
    status = work.status(source_id=source_id, as_of=boundary)
    pending_documents = EvidenceWorkStore(database).list_pending_documents(
        as_of=boundary,
        source_id=source_id,
        limit=2_147_483_647,
        interpreter_version=interpreter_version,
    )
    pending_bundle_identities = {
        (item.document.asset.source_item_id, item.content_version) for item in pending_documents
    }
    unmaterialized_bundles = sum(
        not work.has_interpretation_bundle(
            source_item_id=source_item_id_value,
            content_version=content_version,
            interpreter_version=interpreter_version,
        )
        for source_item_id_value, content_version in pending_bundle_identities
    )
    unmaterialized_discovery_units = (
        0
        if source_id is not None
        else work.missing_discovery_unit_count(
            tuple(
                unit.unit_id
                for unit, _origins in (
                    *_configured_universe_discovery_units(config, created_at=boundary),
                    *_holding_discovery_units(database, config=config, work=work),
                )
            )
        )
    )
    return status.model_copy(
        update={
            "unmaterialized_interpretation_bundles": unmaterialized_bundles,
            "unmaterialized_discovery_units": unmaterialized_discovery_units,
        }
    )


def promote_canonical_claim_discovery(
    *,
    database: Database,
    config: ApplicationConfig,
    canonical_claim_key: str,
    reason: str,
    promoted_at: datetime,
) -> str:
    """Explicitly promote one current canonical-claim state into discovery work."""
    normalized_reason = " ".join(reason.split())
    if not normalized_reason:
        raise ValueError("promotion reason must not be blank")
    claims = ClaimRepository(database, refresh_policy=claim_refresh_policy(config.intelligence))
    projection = next(
        (
            item
            for item in claims.projections_as_of(as_of=promoted_at)
            if item.canonical_claim_key == canonical_claim_key
        ),
        None,
    )
    if projection is None:
        from money_pit.claims.repository import ClaimNotFoundError

        raise ClaimNotFoundError(f"Canonical claim not found: {canonical_claim_key}")
    material_state = {
        "canonical_claim_key": projection.canonical_claim_key,
        "current_status": projection.current_status.value,
        "active_observation_ids": list(projection.active_observation_ids),
        "last_material_change_at": projection.last_material_change_at.isoformat(),
        "freshness_policy_version": projection.freshness_policy_version,
    }
    input_fingerprint = hashlib.sha256(
        json.dumps(material_state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    operator_action_id = hashlib.sha256(
        json.dumps(
            {
                "claim": canonical_claim_key,
                "reason": normalized_reason,
                "promoted_at": promoted_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return IntelligenceWorkRepository(database).promote_canonical_claim_change(
        canonical_claim_key=canonical_claim_key,
        observation_ids=projection.active_observation_ids,
        input_fingerprint=input_fingerprint,
        promoted_at=promoted_at,
        operator_action_id=operator_action_id,
    )


def _configured_universe_discovery_units(
    config: ApplicationConfig,
    *,
    created_at: datetime,
) -> tuple[tuple[DiscoveryUnitRecord, tuple[DiscoveryOriginRecord, ...]], ...]:
    """Build deterministic discovery work implied by current configuration."""
    instruments = tuple(
        dict.fromkeys(
            (
                *config.intelligence.watchlist,
                *config.intelligence.benchmark_constituents,
                *config.intelligence.explicit_proxies.keys(),
                *config.intelligence.explicit_proxies.values(),
                *(
                    instrument
                    for screen in config.intelligence.quantitative_screens
                    for instrument in screen.instruments
                ),
            )
        )
    )
    units: list[tuple[DiscoveryUnitRecord, tuple[DiscoveryOriginRecord, ...]]] = []
    for instrument in instruments:
        payload = {"instrument": instrument, "strategy_version": config.intelligence.version}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        units.append(
            (
                DiscoveryUnitRecord(
                    unit_id=f"universe:{fingerprint}",
                    kind=DiscoveryUnitKind.UNIVERSE_ENTRY,
                    subject_id=instrument,
                    input_fingerprint=fingerprint,
                    source_id=None,
                    created_at=created_at,
                    payload={"observation_ids": [], **payload},
                ),
                (DiscoveryOriginRecord(kind="strategy_version", identifier=config.intelligence.version),),
            )
        )
    return tuple(units)


def _reconcile_holding_discovery_units(
    database: Database,
    *,
    config: ApplicationConfig,
    work: IntelligenceWorkRepository,
    created_at: datetime,
) -> None:
    """Queue holdings from the latest matching durable broker snapshot without broker access."""
    del created_at
    for unit, origins in _holding_discovery_units(database, config=config, work=work):
        work.append_discovery_unit(unit, origins)


def _holding_discovery_units(
    database: Database,
    *,
    config: ApplicationConfig,
    work: IntelligenceWorkRepository,
) -> tuple[tuple[DiscoveryUnitRecord, tuple[DiscoveryOriginRecord, ...]], ...]:
    """Project stable holding-membership work from the latest matching snapshot."""
    strategy = config.require_strategy()
    with database.transaction() as connection:
        rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """SELECT snapshot_id, payload_json FROM portfolio_state_snapshots
            ORDER BY captured_at DESC, snapshot_id DESC"""
            ).fetchall(),
        )
    snapshot = next(
        (
            PortfolioStateSnapshot.model_validate_json(str(cast("object", row["payload_json"])))
            for row in rows
            if PortfolioStateSnapshot.model_validate_json(
                str(cast("object", row["payload_json"]))
            ).payload.broker_environment.value
            == strategy.portfolio_environment.value
        ),
        None,
    )
    if snapshot is None:
        return ()
    units: list[tuple[DiscoveryUnitRecord, tuple[DiscoveryOriginRecord, ...]]] = []
    for position in snapshot.payload.positions:
        holding_state = {
            "account_id": snapshot.payload.account_id,
            "broker_environment": snapshot.payload.broker_environment.value,
            "instrument": position.instrument,
            "universe_layer": UniverseLayer.HOLDING.value,
        }
        fingerprint = hashlib.sha256(
            json.dumps(holding_state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        payload = cast(
            "JsonValue",
            TypeAdapter(JsonValue).validate_python(
                {
                    "observation_ids": [],
                    "holding_state": holding_state,
                }
            ),
        )
        existing = work.discovery_unit(fingerprint)
        units.append(
            (
                DiscoveryUnitRecord(
                    unit_id=fingerprint,
                    kind=DiscoveryUnitKind.UNIVERSE_ENTRY,
                    subject_id=position.instrument,
                    input_fingerprint=fingerprint,
                    source_id=None,
                    created_at=(existing.created_at if existing is not None else snapshot.payload.captured_at),
                    payload=payload,
                ),
                (DiscoveryOriginRecord(kind="portfolio_snapshot", identifier=snapshot.snapshot_id),),
            )
        )
    return tuple(units)


def execute_portfolio_review(
    *,
    database: Database,
    config: ApplicationConfig,
    paths: RepositoryPaths,
    requested_as_of: datetime | None,
    implementation_version: str,
    portfolio_credentials: PortfolioCredentialResolver | None = None,
) -> PortfolioReviewResult:
    """Run one A5-only review from current durable intelligence and broker state."""
    paths.ensure_writable_roots()
    started_at = _utc_now()
    cutoff = requested_as_of or started_at
    run_record = RunRecord(
        run_id=_uuid4_string(),
        requested_as_of=cutoff,
        started_at=started_at,
        known_at=started_at,
        through_stage=Stage.A5.value,
        source_config_hash=canonical_config_hash(config.sources),
        intelligence_config_hash=canonical_config_hash(config.intelligence),
        portfolio_config_hash=canonical_config_hash(config.require_strategy()),
        execution_config_hash=None,
        manifest={"workflow": "portfolio_review"},
    )
    runtime = build_portfolio_runtime(
        database=database,
        config=config,
        reports_root=paths.reports_root,
        processor_versions={"builtin_evidence_processors": implementation_version},
        model_versions={"llm": config.intelligence.llm_model},
        prompt_versions={"portfolio": implementation_version},
        implementation_version=implementation_version,
        portfolio_credentials=(portfolio_credentials or SecretSpecPortfolioResolver.from_environment()),
    )
    run_store = RunRepository(database)
    _ = register_run(paths, run_store, run_record)
    try:
        result = runtime.planning.review(
            PortfolioReviewRequest(
                run_id=run_record.run_id,
                requested_as_of=cutoff,
                execution_eligible=requested_as_of is None,
            )
        )
    except Exception as error:
        failed_at = _utc_now()
        run_store.append_terminal_event(
            RunTerminalEvent(
                run_id=run_record.run_id,
                status=RunTerminalStatus.FAILED,
                completed_at=failed_at,
                known_at=failed_at,
                failure_kind=type(error).__name__[:128],
                failure_detail=RunFailureDetail(durable_record_ids=(), retryable=False),
            )
        )
        raise
    completed_at = _utc_now()
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
    interpretation_version: str = interpretation_policy_version(
        prompt_version=implementation_version,
        model=config.intelligence.llm_model,
    )
    interpreter = InterpretationService(
        evidence=evidence_work,
        admission=admission,
        agent=interpretation_agent,
        implementation_version=interpretation_version,
        clock=dependencies.clock,
    )
    providers = dependencies.research_providers
    research = build_research_runtime(
        database,
        providers,
        AssetStore(assets_root),
        claims=claims,
        interpreter=interpreter,
        credentials=dependencies.inference_credentials,
        model=config.intelligence.llm_model,
        interpretation_prompt_version=implementation_version,
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
    discovery_agent = dependencies.discovery_agent
    research_planning_agent = dependencies.research_planning_agent
    synthesis_agent = dependencies.synthesis_agent
    nodes = HarnessNodes(
        a1=make_interpretation_node(
            evidence=research.evidence_work,
            admission=admission,
            agent=interpretation_agent,
            implementation_version=interpretation_version,
            interpretation_service=interpreter,
            clock=dependencies.clock,
            tracking=dependencies.inference_tracking,
            work_repository=dependencies.intelligence_work,
        ),
        a2=None
        if discovery_agent is None
        else make_discovery_node(
            claims=claims,
            theses=theses,
            research_tasks=research.planned_tasks,
            load_universe=load_universe,
            agent=discovery_agent,
            implementation_version=implementation_version,
            artifact_store=artifact_store,
            allowed_provider_names=providers.names(),
            clock=dependencies.clock,
            tracking=dependencies.inference_tracking,
            work_repository=dependencies.intelligence_work,
        ),
        a3=None
        if research_planning_agent is None
        else make_research_node(
            claims=claims,
            theses=theses,
            tasks=research.planned_tasks,
            runner=research.runner,
            planner=research_planning_agent,
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
            tracking=dependencies.inference_tracking,
            work_repository=dependencies.intelligence_work,
        ),
        a4=None
        if synthesis_agent is None
        else make_synthesis_node(
            claims=claims,
            theses=theses,
            agent=synthesis_agent,
            resolver_version=implementation_version,
            verifier_version=implementation_version,
            synthesis_version=implementation_version,
            admission=admission,
            proxy_relationships={
                key: frozenset({value}) for key, value in config.intelligence.explicit_proxies.items()
            },
            clock=dependencies.clock,
            tracking=dependencies.inference_tracking,
            work_repository=dependencies.intelligence_work,
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
    inference_credentials: InferenceCredentialResolver,
    research_credentials: ResearchCredentialResolver | None = None,
    portfolio_credentials: PortfolioCredentialResolver | None = None,
    execution_credentials: ExecutionCredentialResolver | None = None,
) -> ApplicationDependencies:
    """Resolve production capabilities at the outer application boundary."""
    clock: Callable[[], datetime] = _utc_now
    inference_tracking = InferenceTracking(IntelligenceWorkRepository(database))
    openai: OpenAICredentials = inference_credentials.openai(reason="Run the requested staged investment research")
    discovery_agent = (
        None
        if through is Stage.A1
        else make_discovery_agent(
            openai,
            model=config.intelligence.llm_model,
            tracking=inference_tracking,
        )
    )
    research_planning_agent = (
        make_research_planning_agent(
            openai,
            model=config.intelligence.llm_model,
            tracking=inference_tracking,
        )
        if through in {Stage.A3, Stage.A4, Stage.A5, Stage.A6}
        else None
    )
    synthesis_agent = (
        make_synthesis_agent(
            openai,
            model=config.intelligence.llm_model,
            tracking=inference_tracking,
        )
        if through in {Stage.A4, Stage.A5, Stage.A6}
        else None
    )
    if through in {Stage.A3, Stage.A4, Stage.A5, Stage.A6}:
        if research_credentials is None:
            raise ApplicationDependencyError(f"{through.value} requires research credential authority")
        research_providers = build_configured_research_registry(
            config,
            credentials=research_credentials,
            clock=clock,
        )
    else:
        research_providers = ResearchProviderRegistry()
    portfolio_factory: Callable[[], PortfolioRuntime] | None = None
    if through in {Stage.A5, Stage.A6}:
        if portfolio_credentials is None:
            raise ApplicationDependencyError(f"{through.value} requires portfolio credential authority")
        if through is Stage.A6 and execution_credentials is None:
            raise ApplicationDependencyError("A6 requires execution credential authority")

        def build_capital_runtime() -> PortfolioRuntime:
            return build_portfolio_runtime(
                database=database,
                config=config,
                reports_root=reports_root,
                processor_versions={"builtin_evidence_processors": implementation_version},
                model_versions={"llm": config.intelligence.llm_model},
                prompt_versions={
                    "interpretation": implementation_version,
                    "discovery": implementation_version,
                    "research": implementation_version,
                    "synthesis": implementation_version,
                },
                implementation_version=implementation_version,
                clock=clock,
                portfolio_credentials=portfolio_credentials,
                execution_credentials=execution_credentials,
            )

        portfolio_factory = build_capital_runtime
    return ApplicationDependencies(
        interpretation_agent=make_interpretation_agent(
            openai,
            model=config.intelligence.llm_model,
            tracking=inference_tracking,
        ),
        discovery_agent=discovery_agent,
        research_planning_agent=research_planning_agent,
        synthesis_agent=synthesis_agent,
        research_providers=research_providers,
        instrument_resolver=_configured_instrument_resolver(config),
        clock=clock,
        run_id_factory=_uuid4_string,
        inference_credentials=inference_credentials,
        portfolio_runtime_factory=portfolio_factory,
        inference_tracking=inference_tracking,
        intelligence_work=IntelligenceWorkRepository(database),
    )


def build_configured_research_registry(
    config: ApplicationConfig,
    *,
    credentials: ResearchCredentialResolver,
    clock: Callable[[], datetime] = _utc_now,
) -> ResearchProviderRegistry:
    """Build providers only when both source policy and credentials are configured."""
    registry = ResearchProviderRegistry()
    transport = AddressPinnedHttpTransport()
    brave_definition = _research_source_definition(config, "brave")
    if brave_definition is not None:
        api_key = credentials.brave(reason="Search Brave for bounded financial research").api_key
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
        configured_identity = credentials.edgar(reason="Search EDGAR for primary financial evidence").user_agent
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
        api_key = credentials.fred(reason="Retrieve FRED economic evidence").api_key
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
    approved = {
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
            portfolio_gaps=(),
        )

    return load


def _portfolio_snapshot_as_of(database: Database, as_of: datetime) -> PortfolioStateSnapshot | None:
    with database.transaction() as connection:
        row = cast(
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
