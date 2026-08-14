"""Module composing the persistent read-only research runtime."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from money_pit.claims.repository import ClaimRepository
from money_pit.constants import APP_VERSION
from money_pit.evidence.processors import builtin_evidence_processors
from money_pit.evidence.repository import EvidenceProcessingAttemptRepository
from money_pit.evidence.work import EvidenceWorkStore
from money_pit.pipeline.interpretation import InterpretationService
from money_pit.research.assessment import ClaimVerificationMaterialAssessor
from money_pit.research.memory import DurableResearchRoundRunner
from money_pit.research.memory import PlannedResearchTaskStore
from money_pit.research.registry import ResearchProviderRegistry
from money_pit.research.repository import ResearchRepository
from money_pit.research.service import ResearchService
from money_pit.secrets import InferenceCredentialResolver
from money_pit.sources._shared import utc_now
from money_pit.sources.service import EvidenceRepository
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.sources import SourceRepository


@dataclass(frozen=True)
class ResearchRuntime:
    """Concrete A1-A3 persistence and provider adapters."""

    evidence_work: EvidenceWorkStore
    planned_tasks: PlannedResearchTaskStore
    repository: ResearchRepository
    service: ResearchService
    runner: DurableResearchRoundRunner


def build_research_runtime(
    database: Database,
    providers: ResearchProviderRegistry,
    asset_store: AssetStore,
    *,
    claims: ClaimRepository,
    interpreter: InterpretationService,
    credentials: InferenceCredentialResolver,
    model: str,
    work_repository: IntelligenceWorkRepository,
    interpretation_prompt_version: str = APP_VERSION,
    clock: Callable[[], datetime] = utc_now,
) -> ResearchRuntime:
    """Compose persistent evidence work and bounded research adapters."""
    repository = ResearchRepository(database)
    planned_tasks = PlannedResearchTaskStore(database)
    service = ResearchService(
        providers,
        repository,
        SourceRepository(database),
        EvidenceRepository(database),
        builtin_evidence_processors(credentials, model=model),
        EvidenceProcessingAttemptRepository(database),
        asset_store,
        work_repository,
        evidence_work=EvidenceWorkStore(database),
        interpretation_model=model,
        interpretation_prompt_version=interpretation_prompt_version,
        clock=clock,
    )
    return ResearchRuntime(
        evidence_work=EvidenceWorkStore(database),
        planned_tasks=planned_tasks,
        repository=repository,
        service=service,
        runner=DurableResearchRoundRunner(
            service,
            repository,
            planned_tasks,
            work_repository,
            interpreter=interpreter,
            material_assessor=ClaimVerificationMaterialAssessor(claims),
        ),
    )
