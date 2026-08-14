from datetime import UTC
from datetime import datetime
from datetime import timedelta
from itertools import count
from pathlib import Path
from typing import TypeVar
from typing import cast

import pytest

from money_pit import composition as composition_module
from money_pit.agents.inference import InferenceInvocationContext
from money_pit.agents.inference import InferenceResult
from money_pit.agents.inference import InferenceUsage
from money_pit.claims.repository import ClaimRepository
from money_pit.composition import ApplicationDependencies
from money_pit.composition import build_application_runtime
from money_pit.composition import execute_portfolio_review
from money_pit.composition import initial_state
from money_pit.config import ApplicationConfig
from money_pit.config import ClaimFreshnessPolicyConfig
from money_pit.config import ClaimFreshnessRuleConfig
from money_pit.config import ExecutionConfig
from money_pit.config import HorizonPolicy
from money_pit.config import ResearchBudgetConfig
from money_pit.config import ReturnBoundPolicy
from money_pit.config import StrategyConfig
from money_pit.config import StrategyIntelligenceConfig
from money_pit.config import claim_refresh_policy
from money_pit.contracts import CandidateThesisDraft
from money_pit.contracts import ClaimObservationDraft
from money_pit.contracts import DiscoveryDraft
from money_pit.contracts import DiscoveryRequest
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import InterpretationRequest
from money_pit.contracts import ResearchPlanningRequest
from money_pit.contracts import ResearchRoundPlan
from money_pit.contracts import ResearchTaskDraft
from money_pit.contracts import ResolutionDraft
from money_pit.contracts import SynthesisDraft
from money_pit.contracts import SynthesisRequest
from money_pit.contracts import ThesisRevisionDraft
from money_pit.contracts import VerificationDraft
from money_pit.evidence.repository import EvidenceProcessingAttemptRepository
from money_pit.execution_control.fills import ExecutionPhase
from money_pit.execution_control.fills import FillObservation
from money_pit.execution_control.gateway import CurrentStateChecks
from money_pit.execution_control.gateway import ExecutionGatewayDependencies
from money_pit.execution_control.gateway import execute_plan
from money_pit.execution_control.models import ConfirmedFill
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.orders import OrderIntent
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.execution_control.repository import TurnoverReservationRepository
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.orchestration import IntelligenceNodes
from money_pit.pipeline.orchestration import IntelligenceStage
from money_pit.pipeline.orchestration import IntelligenceUpdateReport
from money_pit.pipeline.orchestration import run_intelligence_update
from money_pit.plans.lifecycle import approve_plan
from money_pit.plans.repository import PortfolioPlanRepository
from money_pit.portfolio.composition import PortfolioReadDependencies
from money_pit.portfolio.composition import PortfolioRuntime
from money_pit.portfolio.composition import build_portfolio_runtime
from money_pit.portfolio.eligibility import SupportedInstrumentKind
from money_pit.portfolio.outcome_repository import SqliteOutcomeRepository
from money_pit.portfolio.providers import InstrumentLiquidity
from money_pit.portfolio.providers import InstrumentRisk
from money_pit.portfolio.providers import LiquiditySnapshot
from money_pit.portfolio.providers import LiquiditySnapshotPayload
from money_pit.portfolio.providers import RiskSnapshot
from money_pit.portfolio.providers import RiskSnapshotPayload
from money_pit.portfolio.snapshots import MarketQuote
from money_pit.portfolio.snapshots import MarketStatePayload
from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStatePosition
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.portfolio.theses import ThesisRepository
from money_pit.portfolio.universe import ConfiguredInstrumentResolver
from money_pit.research.providers import SearchHit
from money_pit.research.providers import WebResearchProvider
from money_pit.research.registry import ResearchProviderRegistry
from money_pit.runs.manifest import register_run
from money_pit.runs.paths import RepositoryPaths
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.instrument import InstrumentExposureClass
from money_pit.schemas.outcomes import OutcomeBoundary
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.research import ResearchStopReason
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import RunTerminalEvent
from money_pit.schemas.runs import RunTerminalStatus
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.schemas.tax import TaxLotSnapshot
from money_pit.schemas.theses import ScenarioOutcome
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.schemas.universe import UniverseLayer
from money_pit.secrets import SecretSpecInferenceResolver
from money_pit.secrets import SecretSpecPortfolioResolver
from money_pit.sources.http import HttpResponse
from money_pit.sources.local import local_text_connector
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.service import EvidenceRepository
from money_pit.sources.service import SourceSyncService
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.runs import RunRepository
from money_pit.storage.sources import SourceRepository


_RUN_ID = "4fa85f64-5717-4562-b3fc-2c963f66afa6"
_INITIAL_CLAIM = "NEW backlog grew 20 percent year over year."
_PRIMARY_CLAIM = "NEW reported the backlog increase in its quarterly filing."


def _execution_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        policy_version="execution-test",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        allowed_asset_classes=(TradableAssetClass.US_EQUITY, TradableAssetClass.US_ETF),
        maximum_order_notional=1_000,
        maximum_daily_turnover=0.3,
    )


class _SearchBackend:
    def search(self, query_text: str, *, maximum_results: int) -> tuple[SearchHit, ...]:
        del query_text, maximum_results
        return (SearchHit(canonical_uri="https://issuer.test/filing", title="Quarterly filing"),)


class _ResearchTransport:
    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        del url, maximum_bytes, timeout_seconds
        return HttpResponse(
            _PRIMARY_CLAIM.encode(),
            "text/plain",
            "https://issuer.test/filing",
        )


class _OfflinePortfolioProvider:
    def __init__(self, captured_at: datetime) -> None:
        self._captured_at: datetime = captured_at

    def snapshot(self) -> PortfolioStateSnapshot:
        return PortfolioStateSnapshot.from_payload(
            PortfolioStatePayload(
                account_id="paper-account",
                captured_at=self._captured_at,
                available_cash=500,
                positions=(
                    PortfolioStatePosition(
                        instrument="SPY",
                        quantity=1,
                        market_price=500,
                        market_value=500,
                    ),
                ),
                open_order_ids=(),
            )
        )

    def instrument_authority(self, instrument: str) -> tuple[SupportedInstrumentKind, bool]:
        assert instrument in {"NEW", "SPY"}
        return (
            SupportedInstrumentKind.US_ETF if instrument == "SPY" else SupportedInstrumentKind.US_EQUITY,
            True,
        )


class _OfflineMarketProvider:
    def __init__(self, captured_at: datetime) -> None:
        self._captured_at: datetime = captured_at

    def snapshot(self, instruments: tuple[str, ...]) -> MarketStateSnapshot:
        return MarketStateSnapshot.from_payload(
            MarketStatePayload(
                captured_at=self._captured_at,
                quotes=tuple(
                    MarketQuote(
                        instrument=instrument,
                        price=100 if instrument == "NEW" else 500,
                        observed_at=self._captured_at,
                        source="offline-market",
                    )
                    for instrument in instruments
                ),
            )
        )


class _OfflineTaxProvider:
    def __init__(self, captured_at: datetime) -> None:
        self._captured_at: datetime = captured_at

    def snapshot(self) -> TaxLotSnapshot:
        return TaxLotSnapshot(
            snapshot_id="tax-snapshot",
            captured_at=self._captured_at,
            lots=(),
            complete_for_known_accounts=True,
            unknown_external_activity=False,
        )


class _OfflineRiskProvider:
    def __init__(self, captured_at: datetime) -> None:
        self._captured_at: datetime = captured_at

    def snapshot(self, instruments: tuple[str, ...]) -> RiskSnapshot:
        assert instruments == ("NEW", "SPY")
        return RiskSnapshot.from_payload(
            RiskSnapshotPayload(
                captured_at=self._captured_at,
                observations=(
                    InstrumentRisk(
                        instrument="NEW",
                        sector="technology",
                        factor_loadings={"market": 1.1},
                        covariance={"NEW": 0.08, "SPY": 0.02},
                    ),
                    InstrumentRisk(
                        instrument="SPY",
                        sector="broad_market",
                        factor_loadings={"market": 1},
                        covariance={"NEW": 0.02, "SPY": 0.04},
                    ),
                ),
            )
        )


class _OfflineLiquidityProvider:
    def __init__(self, captured_at: datetime) -> None:
        self._captured_at: datetime = captured_at

    def snapshot(self, instruments: tuple[str, ...]) -> LiquiditySnapshot:
        return LiquiditySnapshot.from_payload(
            LiquiditySnapshotPayload(
                captured_at=self._captured_at,
                observations=tuple(
                    InstrumentLiquidity(
                        instrument=instrument,
                        average_daily_notional=10_000_000 if instrument == "NEW" else 100_000_000,
                        maximum_participation_rate=0.01,
                        estimated_slippage_bps=5 if instrument == "NEW" else 1,
                        tradable=True,
                    )
                    for instrument in instruments
                ),
            )
        )


_OutputT = TypeVar("_OutputT")


def _inference_result(output: _OutputT) -> InferenceResult[_OutputT]:
    return InferenceResult(output=output, usage=InferenceUsage(), request_hash="0" * 64)


def _interpret(
    request: InterpretationRequest, *, context: InferenceInvocationContext
) -> InferenceResult[InterpretationDraft]:
    del context
    is_primary = request.source_item_id.startswith("research.primary:")
    return _inference_result(
        InterpretationDraft(
            observations=(
                ClaimObservationDraft(
                    claim_text=_PRIMARY_CLAIM if is_primary else _INITIAL_CLAIM,
                    claim_kind=ClaimKind.FACTUAL,
                    category=ClaimCategory.FUNDAMENTAL,
                    evidence_aliases=(request.evidence[0].alias,),
                    asserted_at=request.context_known_at,
                    effective_from=request.context_known_at,
                    review_at=request.context_known_at + timedelta(days=30),
                    valid_until=request.context_known_at + timedelta(days=90),
                    horizon_class=HorizonClass.TACTICAL,
                    instruments=("NEW",),
                    causal_mechanisms=("backlog conversion",),
                    regime_assumptions=("stable demand",),
                ),
            ),
        )
    )


def _discover(request: DiscoveryRequest, *, context: InferenceInvocationContext) -> InferenceResult[DiscoveryDraft]:
    del context
    assert "primary" in request.allowed_provider_names
    return _inference_result(
        DiscoveryDraft(
            candidates=(
                CandidateThesisDraft(
                    subject="NEW backlog conversion",
                    direction=ThesisDirection.LONG,
                    instrument_reference="NEW",
                    horizon_class=HorizonClass.TACTICAL,
                    discovery_basis=DiscoveryBasis(
                        universe_layer=UniverseLayer.WATCHLIST,
                        universe_reference="NEW",
                    ),
                    causal_mechanisms=("backlog conversion",),
                    regime_assumptions=("stable demand",),
                ),
            ),
            research_tasks=(
                ResearchTaskDraft(
                    candidate_subject="NEW backlog conversion",
                    provider="primary",
                    query="NEW quarterly filing backlog",
                    purpose="Verify the candidate's material backlog premise.",
                    maximum_results=1,
                ),
            ),
        )
    )


def _reject_recursive_discovery(
    request: DiscoveryRequest, *, context: InferenceInvocationContext
) -> InferenceResult[DiscoveryDraft]:
    del request, context
    raise AssertionError("Candidate verification must not recursively invoke discovery.")


def _plan_research(
    request: ResearchPlanningRequest, *, context: InferenceInvocationContext
) -> InferenceResult[ResearchRoundPlan]:
    del context
    assert request.progress.completed_wave_count >= 1
    return _inference_result(ResearchRoundPlan(stop_reason=ResearchStopReason.UNRESOLVED))


def _synthesize(request: SynthesisRequest, *, context: InferenceInvocationContext) -> InferenceResult[SynthesisDraft]:
    del context
    initial = next(item for item in request.observations if item.claim_text == _INITIAL_CLAIM)
    primary = next(item for item in request.observations if item.claim_text == _PRIMARY_CLAIM)
    supporting_alias = next(
        item.alias for item in request.research_context.alias_bindings if item.source_item_id == primary.source_item_id
    )
    candidate = request.candidates[0]
    return _inference_result(
        SynthesisDraft(
            resolutions=(
                ResolutionDraft(
                    subject_observation_id=initial.observation_id,
                    relation="distinct",
                    rationale="This is the first bounded interpretation of the material premise.",
                ),
                ResolutionDraft(
                    subject_observation_id=primary.observation_id,
                    relation="distinct",
                    rationale="The filing statement is a separately retained factual observation.",
                ),
            ),
            verifications=(
                VerificationDraft(
                    observation_id=initial.observation_id,
                    status="supported",
                    supporting_evidence_aliases=(supporting_alias,),
                ),
                VerificationDraft(
                    observation_id=primary.observation_id,
                    status="unresolved",
                    limitations=("The originating filing cannot independently verify itself.",),
                ),
            ),
            revisions=(
                ThesisRevisionDraft(
                    promoted_from_candidate_id=candidate.candidate_thesis_id,
                    subject=candidate.subject,
                    instrument="NEW",
                    direction=ThesisDirection.LONG,
                    horizon_class=HorizonClass.TACTICAL,
                    effective_from=request.context_known_at,
                    review_at=request.context_known_at + timedelta(days=30),
                    valid_until=request.context_known_at + timedelta(days=90),
                    scenario_distribution=(ScenarioOutcome(name="base", probability=1.0, expected_return=0.08),),
                    invalidation_rules=("Backlog conversion falls below plan.",),
                    supporting_observation_ids=(initial.observation_id,),
                    causal_mechanisms=("backlog conversion",),
                    regime_assumptions=("stable demand",),
                    confidence=0.7,
                    reasoning="Primary evidence supports the factual anchor for a new non-held thesis.",
                ),
            ),
            contributions=(),
        )
    )


def _strategy(tmp_path: Path) -> StrategyConfig:
    freshness_rule = ClaimFreshnessRuleConfig(review_interval_days=7, freshness_days=30)
    return StrategyConfig(
        version="strategy-test",
        llm_model="test-model",
        portfolio_environment=BrokerEnvironment.PAPER,
        plan_ttl_seconds=1_800,
        watchlist=("NEW",),
        benchmark_constituents=("SPY",),
        benchmark_id="configured-benchmark",
        benchmark_provider="offline-market",
        benchmark_weights={"SPY": 1.0},
        sector_taxonomy={"SPY": "broad_market", "NEW": "technology"},
        instrument_asset_classes={"SPY": TradableAssetClass.US_ETF, "NEW": TradableAssetClass.US_EQUITY},
        instrument_exposure_classes={
            "SPY": InstrumentExposureClass.BROAD_MARKET_EQUITY_ETF,
            "NEW": InstrumentExposureClass.SINGLE_STOCK,
        },
        factor_loadings={"SPY": {"market": 1.0}, "NEW": {"market": 1.1}},
        default_position_weight_limit=0.5,
        maximum_equity_exposure=0.9,
        maximum_single_stock_exposure=0.5,
        maximum_thematic_etf_exposure=0.4,
        sector_weight_limit=0.7,
        factor_weight_limit=1.0,
        correlated_exposure_limit=0.7,
        cash_minimum=0.1,
        turnover_limit=0.3,
        position_change_limit=0.25,
        minimum_trade_notional=10.0,
        maximum_slippage_fraction=0.01,
        minimum_average_daily_notional=1_000_000.0,
        maximum_daily_volume_participation=0.01,
        market_history_period="1y",
        tax_lot_ledger_path=tmp_path / "lots.json",
        tax_lot_policy="fifo",
        expected_return_calibration_version="calibration-1",
        expected_return_bounds_version="bounds-1",
        expected_return_uncertainty_multiplier=0.8,
        scenario_return_floor=-1.0,
        scenario_return_ceiling=1.0,
        calibrated_return_floor=-1.0,
        calibrated_return_ceiling=1.0,
        return_bound_policy=ReturnBoundPolicy.REJECT,
        risk_aversion=0.5,
        turnover_penalty=0.01,
        tax_penalty=1.0,
        feasibility_tolerance=1e-6,
        horizons={
            "event": HorizonPolicy(minimum_days=0, maximum_days=14, review_interval_days=1),
            "tactical": HorizonPolicy(minimum_days=15, maximum_days=90, review_interval_days=7),
            "medium_term": HorizonPolicy(minimum_days=91, maximum_days=365, review_interval_days=30),
            "structural": HorizonPolicy(minimum_days=366, review_interval_days=90),
        },
        research_budget=ResearchBudgetConfig(maximum_rounds=3, maximum_queries=12, maximum_fetches=24),
        claim_freshness=ClaimFreshnessPolicyConfig(
            version="freshness-1",
            rules={category: dict.fromkeys(HorizonClass, freshness_rule) for category in ClaimCategory},
        ),
    )


def test_incremental_intelligence_then_a5_review_and_approved_a6_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.txt"
    _ = source_path.write_text(_INITIAL_CLAIM, encoding="utf-8")
    source = SourceDefinition(
        source_id="manual",
        adapter_name="local_text",
        locator=str(source_path),
        provenance_group="originating-commentary",
        allowed_uses=(AllowedUse.INTERPRETATION, AllowedUse.THESIS_GENERATION),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.COMMENTARY),),
    )
    source_registry = SourceRegistryDocument(version="0.0.2", sources=(source,))
    strategy = _strategy(tmp_path)
    config = ApplicationConfig(
        sources=source_registry,
        intelligence=StrategyIntelligenceConfig.model_validate(strategy.model_dump()),
        strategy=strategy,
        execution=ExecutionConfig(
            policy_version="execution-test",
            broker_environment=BrokerEnvironment.PAPER,
            execution_mode=ExecutionMode.APPROVAL_REQUIRED,
            maximum_order_notional=1_000.0,
            maximum_daily_turnover=0.3,
            maximum_market_drift_fraction=0.02,
            poll_interval_seconds=0.01,
            poll_timeout_seconds=1.0,
        ),
    )
    execution_config = config.require_execution()
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    paths = RepositoryPaths.from_data_root(tmp_path / "data")
    adapters = AdapterRegistry()
    adapters.register("local_text", local_text_connector)
    sync = SourceSyncService(
        source_registry,
        adapters,
        SourceRepository(database),
        EvidenceRepository(database),
        AssetStore(paths.assets_root),
        attempt_repository=EvidenceProcessingAttemptRepository(database),
    )
    _ = sync.register_definitions()
    _ = sync.sync(source.source_id)
    run_time = datetime.now(tz=UTC)
    primary_definition = SourceDefinition(
        source_id="research.primary",
        adapter_name="research",
        locator="https://issuer.test",
        provenance_group="issuer-primary",
        allowed_uses=(
            AllowedUse.INTERPRETATION,
            AllowedUse.FACTUAL_VERIFICATION,
            AllowedUse.THESIS_GENERATION,
            AllowedUse.PORTFOLIO_DECISION,
        ),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.AUTHORITATIVE_PRIMARY),),
    )
    providers = ResearchProviderRegistry()
    providers.register(
        WebResearchProvider(
            "primary",
            primary_definition,
            _SearchBackend(),
            _ResearchTransport(),
            clock=lambda: run_time,
        )
    )
    submitted_orders: list[OrderIntent] = []

    def portfolio_runtime_factory() -> PortfolioRuntime:
        return build_portfolio_runtime(
            database=database,
            config=config,
            reports_root=paths.reports_root,
            processor_versions={"text": "integration-002"},
            model_versions={"synthesis": "deterministic"},
            prompt_versions={"synthesis": "integration-002"},
            implementation_version="integration-002",
            clock=lambda: run_time,
            portfolio_credentials=SecretSpecPortfolioResolver(tmp_path / "unused-secretspec.toml"),
            read_dependencies=PortfolioReadDependencies(
                portfolio=_OfflinePortfolioProvider(run_time),
                market=_OfflineMarketProvider(run_time),
                risk=_OfflineRiskProvider(run_time),
                liquidity=_OfflineLiquidityProvider(run_time),
                tax_lots=_OfflineTaxProvider(run_time),
            ),
        )

    work = IntelligenceWorkRepository(database)
    dependencies = ApplicationDependencies(
        interpretation_agent=_interpret,
        discovery_agent=_discover,
        research_planning_agent=_plan_research,
        synthesis_agent=_synthesize,
        research_providers=providers,
        instrument_resolver=ConfiguredInstrumentResolver(frozenset({"NEW", "SPY"})),
        clock=lambda: run_time,
        run_id_factory=lambda: _RUN_ID,
        inference_credentials=SecretSpecInferenceResolver(tmp_path / "unused-secretspec.toml"),
        intelligence_work=work,
    )
    intelligence_runtime = build_application_runtime(
        database=database,
        config=config,
        assets_root=paths.assets_root,
        implementation_version="integration-002",
        through=Stage.A4,
        dependencies=dependencies,
    )
    run_ids = (
        _RUN_ID,
        "8f24f64a-62c8-4a3d-8d1a-6d6d61a26349",
        "c15c250b-80c4-4f55-8aac-2ed84d5eac60",
        "d203ead3-6b46-4e4c-85fc-727af1051d8f",
    )
    reports: list[IntelligenceUpdateReport] = []
    run_store = RunRepository(database)
    for run_id in run_ids:
        run_record = RunRecord(
            run_id=run_id,
            requested_as_of=run_time,
            started_at=run_time,
            known_at=run_time,
            through_stage=Stage.A4.value,
            source_config_hash="a" * 64,
            intelligence_config_hash="b" * 64,
        )
        run_dir = register_run(paths, run_store, run_record)
        report = run_intelligence_update(
            initial_state(
                run_id=run_id,
                run_dir=run_dir,
                requested_as_of=run_time,
                run_started_at=run_time,
                requested_as_of_explicit=False,
                config=config,
                through=Stage.A4,
                source_id=source.source_id,
            ),
            nodes=IntelligenceNodes(
                a1=intelligence_runtime.nodes.a1,
                a2=intelligence_runtime.nodes.a2,
                a3=intelligence_runtime.nodes.a3,
                a4=intelligence_runtime.nodes.a4,
            ),
            through=IntelligenceStage.SYNTHESIS,
            run_record=run_record,
            work=work,
        )
        reports.append(report)
        run_store.append_terminal_event(
            RunTerminalEvent(
                run_id=run_id,
                status=RunTerminalStatus.COMPLETED,
                completed_at=run_time,
                known_at=run_time,
            )
        )
        if ThesisRepository(database).revisions_as_of(as_of=run_time):
            break
    assert reports[0].remaining.active_research_jobs == 1
    assert all(report.completed_stages == ("A1", "A2", "A3", "A4") for report in reports)

    no_recursion_runtime = build_application_runtime(
        database=database,
        config=config,
        assets_root=paths.assets_root,
        implementation_version="integration-002",
        through=Stage.A4,
        dependencies=ApplicationDependencies(
            interpretation_agent=_interpret,
            discovery_agent=_reject_recursive_discovery,
            research_planning_agent=_plan_research,
            synthesis_agent=_synthesize,
            research_providers=providers,
            instrument_resolver=ConfiguredInstrumentResolver(frozenset({"NEW", "SPY"})),
            clock=lambda: run_time,
            run_id_factory=lambda: "472d1aeb-60c8-4980-8ef3-a29c39c91584",
            inference_credentials=SecretSpecInferenceResolver(tmp_path / "unused-secretspec.toml"),
            intelligence_work=work,
        ),
    )
    no_recursion_run = RunRecord(
        run_id="472d1aeb-60c8-4980-8ef3-a29c39c91584",
        requested_as_of=run_time,
        started_at=run_time,
        known_at=run_time,
        through_stage=Stage.A4.value,
        source_config_hash="a" * 64,
        intelligence_config_hash="b" * 64,
    )
    no_recursion_dir = register_run(paths, run_store, no_recursion_run)
    no_recursion_report = run_intelligence_update(
        initial_state(
            run_id=no_recursion_run.run_id,
            run_dir=no_recursion_dir,
            requested_as_of=run_time,
            run_started_at=run_time,
            requested_as_of_explicit=False,
            config=config,
            through=Stage.A4,
            source_id=source.source_id,
        ),
        nodes=IntelligenceNodes(
            a1=no_recursion_runtime.nodes.a1,
            a2=no_recursion_runtime.nodes.a2,
            a3=no_recursion_runtime.nodes.a3,
            a4=no_recursion_runtime.nodes.a4,
        ),
        through=IntelligenceStage.SYNTHESIS,
        run_record=no_recursion_run,
        work=work,
    )
    assert no_recursion_report.completed.discovery_units == 0

    portfolio_runtime = portfolio_runtime_factory()

    def _portfolio_runtime_override(**_kwargs: object) -> PortfolioRuntime:
        return portfolio_runtime

    monkeypatch.setattr(composition_module, "build_portfolio_runtime", _portfolio_runtime_override)
    monkeypatch.setattr(composition_module, "_utc_now", lambda: run_time)
    monkeypatch.setattr(
        composition_module,
        "_uuid4_string",
        lambda: "fbb2319c-2080-4ea4-838f-a9b33949e6cf",
    )
    review = execute_portfolio_review(
        database=database,
        config=config,
        paths=paths,
        requested_as_of=None,
        implementation_version="integration-002",
        portfolio_credentials=SecretSpecPortfolioResolver(tmp_path / "unused-secretspec.toml"),
    )

    claims = ClaimRepository(database, refresh_policy=claim_refresh_policy(strategy))
    revisions = ThesisRepository(database).revisions_as_of(as_of=run_time)
    with database.transaction() as connection:
        recursive_discovery_count = cast(
            "int",
            connection.execute(
                """SELECT count(*) FROM discovery_units
                WHERE unit_kind = 'canonical_claim'"""
            ).fetchone()[0],
        )
    plan_id = review.plan_id
    plan = PortfolioPlanRepository(database).get(plan_id)
    assert plan is not None
    assert plan.payload.execution_config_hash is not None
    approval = approve_plan(
        plan,
        decision_id="approval-integration",
        decided_at=run_time,
        decided_by="integration-operator",
        execution_config_hash=plan.payload.execution_config_hash,
        execution_policy=_execution_policy(),
        account_id=plan.payload.account_id,
        committed_turnover_at_approval=0.0,
    )
    authority = SqliteExecutionAuthorityRepository(database)
    authority.enable(
        ExecutionControlState(
            disabled=False,
            changed_at=run_time,
            actor="integration-operator",
            policy_version=execution_config.policy_version,
        )
    )
    authority.append(approval)
    identifiers = count(1)

    def current(
        _plan: PortfolioPlan,
        _confirmed_fills: tuple[ConfirmedFill, ...],
    ) -> bool:
        return True

    def place_order(intent: OrderIntent) -> str:
        submitted_orders.append(intent)
        return f"paper-order-{len(submitted_orders)}"

    execution = ExecutionGatewayDependencies(
        plans=authority,
        decisions=authority,
        kill_switch=authority,
        claims=authority,
        journal=authority,
        current_state=CurrentStateChecks(
            portfolio_unchanged=current,
            market_unchanged=current,
            cash_unchanged=current,
            open_orders_unchanged=current,
            evidence_fresh=current,
            tax_state_unchanged=current,
            account_ready=current,
            market_fresh=current,
        ),
        execution_config_hash=plan.payload.execution_config_hash,
        committed_turnover_excluding_plan=lambda _plan_id, _as_of: 0.0,
        turnover_reservations=TurnoverReservationRepository(database),
        order_placer_factory=lambda _environment: place_order,
        observe_fill=lambda _identity: FillObservation(
            phase=ExecutionPhase.FILLED,
            status="filled",
            filled_qty=1,
            filled_avg_price=100,
            realized_notional=100,
        ),
        clock=lambda: run_time,
        event_id_factory=lambda: f"event-{next(identifiers)}",
        poll_interval_seconds=0.01,
        poll_timeout_seconds=1,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0,
    )
    receipt = execute_plan(
        plan.payload.plan_id,
        _execution_policy(),
        execution,
    )
    outcome_schedules = SqliteOutcomeRepository(database).due(
        as_of=run_time + timedelta(days=90),
    )
    assert (
        reports[-1].completed_stages,
        recursive_discovery_count,
        len(claims.observations_as_of(as_of=run_time)),
        revisions[0].instrument,
        revisions[0].promoted_from_candidate_id is not None,
        tuple(trade.instrument for trade in plan.payload.proposed_trades),
        approval.plan_hash,
        receipt.completed_trade_identities,
        tuple(item.boundary for item in outcome_schedules),
        {(item.thesis_revision_id, item.plan_id, item.plan_hash) for item in outcome_schedules},
    ) == (
        ("A1", "A2", "A3", "A4"),
        0,
        2,
        "NEW",
        True,
        ("NEW", "SPY"),
        plan.plan_hash,
        receipt.submitted_trade_identities,
        (OutcomeBoundary.REVIEW, OutcomeBoundary.HORIZON),
        {(revisions[0].revision_id, plan.payload.plan_id, plan.plan_hash)},
    )
    assert submitted_orders[0].symbol == "NEW"


def test_build_portfolio_runtime_uses_injected_read_providers_without_external_credentials(tmp_path: Path) -> None:
    strategy = _strategy(tmp_path)
    config = ApplicationConfig(
        sources=SourceRegistryDocument(version="0.0.2", sources=()),
        intelligence=StrategyIntelligenceConfig.model_validate(strategy.model_dump()),
        strategy=strategy,
        execution=ExecutionConfig(
            policy_version="execution-test",
            broker_environment=BrokerEnvironment.PAPER,
            execution_mode=ExecutionMode.APPROVAL_REQUIRED,
            maximum_order_notional=1_000,
            maximum_daily_turnover=0.3,
            maximum_market_drift_fraction=0.02,
            poll_interval_seconds=0.01,
            poll_timeout_seconds=1.0,
        ),
    )
    database = Database(tmp_path / "composition.sqlite3")
    database.initialize()
    captured_at = datetime(2026, 8, 9, 14, tzinfo=UTC)
    portfolio = _OfflinePortfolioProvider(captured_at)
    market = _OfflineMarketProvider(captured_at)
    tax_lots = _OfflineTaxProvider(captured_at)
    reads = PortfolioReadDependencies(
        portfolio=portfolio,
        market=market,
        risk=_OfflineRiskProvider(captured_at),
        liquidity=_OfflineLiquidityProvider(captured_at),
        tax_lots=tax_lots,
    )

    runtime = build_portfolio_runtime(
        database=database,
        config=config,
        reports_root=tmp_path / "reports",
        processor_versions={"test": "1"},
        model_versions={"test": "1"},
        prompt_versions={"test": "1"},
        implementation_version="integration-002",
        portfolio_credentials=SecretSpecPortfolioResolver(tmp_path / "unused-secretspec.toml"),
        read_dependencies=reads,
    )

    assert (runtime.portfolio, runtime.market, runtime.tax_lots, runtime.execution is not None) == (
        portfolio,
        market,
        tax_lots,
        False,
    )
