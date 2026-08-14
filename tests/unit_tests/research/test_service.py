import hashlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest
from typing_extensions import override


if TYPE_CHECKING:
    import sqlite3

from money_pit.claims.projection import ClaimFreshnessRule
from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.repository import ClaimRepository
from money_pit.evidence.media import FrameReading
from money_pit.evidence.media import MediaAnalysis
from money_pit.evidence.media import MediaEvidenceProcessor
from money_pit.evidence.media import TimedTranscriptSegment
from money_pit.evidence.processors import EvidenceProcessorRegistry
from money_pit.evidence.repository import EvidenceProcessingAttemptRepository
from money_pit.pipeline.interpretation import InterpretationService
from money_pit.portfolio.theses import ThesisRepository
from money_pit.research.composition import ResearchRuntime
from money_pit.research.composition import build_research_runtime
from money_pit.research.errors import HistoricalResearchUnavailableError
from money_pit.research.protocol import ResearchProviderCapabilities
from money_pit.research.registry import ResearchProviderRegistry
from money_pit.research.repository import ResearchRepository
from money_pit.research.service import ResearchService
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.research import CandidateThesisResearchScope
from money_pit.schemas.research import ResearchDiscoveryBatch
from money_pit.schemas.research import ResearchDiscoveryResult
from money_pit.schemas.research import ResearchFetch
from money_pit.schemas.research import ResearchQuery
from money_pit.schemas.research import ResearchSession
from money_pit.schemas.research import ResearchTask
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.secrets import SecretSpecInferenceResolver
from money_pit.sources._shared import source_definition_hash
from money_pit.sources.service import EvidenceRepository
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.sources import SourceRepository


_CUTOFF = datetime(2098, 7, 1, tzinfo=UTC)
_DECISION_AT = datetime(2099, 8, 9, tzinfo=UTC)
_VIDEO = b"research-video"
_FRAME = b"research-frame"
_RUN_ID = "4fa85f64-5717-4562-b3fc-2c963f66afa6"


def _seed_run(database: Database) -> None:
    with database.transaction() as connection:
        _ = connection.execute(
            """
                INSERT INTO runs (
                    run_id, requested_as_of, started_at, known_at, through_stage,
                    source_config_hash, intelligence_config_hash, manifest_json
                ) VALUES (?, ?, ?, ?, 'A3', ?, ?, '{}')
                """,
            (
                _RUN_ID,
                _CUTOFF.isoformat(),
                _DECISION_AT.isoformat(),
                _DECISION_AT.isoformat(),
                "a" * 64,
                "b" * 64,
            ),
        )


def _claims(database: Database) -> ClaimRepository:
    rule = ClaimFreshnessRule(
        review_interval=timedelta(days=7),
        freshness_interval=timedelta(days=30),
    )
    return ClaimRepository(
        database,
        refresh_policy=ClaimRefreshPolicy(
            policy_version="claim-freshness-1",
            rules={category: dict.fromkeys(HorizonClass, rule) for category in ClaimCategory},
        ),
    )


def _interpreter() -> InterpretationService:
    return cast("InterpretationService", object())


class _CurrentOnlyProvider:
    name: str = "current-web"
    capabilities: ResearchProviderCapabilities = ResearchProviderCapabilities()
    source_definition: SourceDefinition = SourceDefinition(
        source_id="research.current-web",
        adapter_name="research",
        locator="https://example.test",
        provenance_group="example-publisher",
        allowed_uses=(AllowedUse.FACTUAL_VERIFICATION,),
        trust_settings=(
            SourceTrustSetting(
                category=TrustCategory.FACTUAL,
                level=TrustLevel.INDEPENDENT_SECONDARY,
            ),
        ),
    )

    def source_definition_for(self, result: ResearchDiscoveryResult) -> SourceDefinition:
        assert result.provider == self.name
        return self.source_definition

    def search(self, query: ResearchQuery) -> ResearchDiscoveryBatch:
        raise AssertionError(f"historical run called current-only search: {query}")

    def fetch(self, result: ResearchDiscoveryResult) -> RawArtifact:
        raise AssertionError(f"historical run called current-only fetch: {result}")

    def search_as_of(
        self,
        query: ResearchQuery,
        *,
        requested_as_of: datetime,
    ) -> ResearchDiscoveryBatch:
        raise AssertionError((query, requested_as_of))

    def fetch_as_of(
        self,
        result: ResearchDiscoveryResult,
        *,
        requested_as_of: datetime,
    ) -> RawArtifact:
        raise AssertionError((result, requested_as_of))


class _CertifiedProvider:
    name: str = "certified"
    capabilities: ResearchProviderCapabilities = ResearchProviderCapabilities(
        point_in_time_certified=True,
        availability_is_verifiable=True,
    )
    source_definition: SourceDefinition = SourceDefinition(
        source_id="research.certified",
        adapter_name="research",
        locator="https://archive.example.test",
        provenance_group="archive",
        allowed_uses=(AllowedUse.FACTUAL_VERIFICATION,),
        trust_settings=(
            SourceTrustSetting(
                category=TrustCategory.FACTUAL,
                level=TrustLevel.INDEPENDENT_SECONDARY,
            ),
        ),
    )

    def __init__(self) -> None:
        self.search_cutoffs: list[datetime] = []
        self.fetch_cutoffs: list[datetime] = []

    def source_definition_for(self, result: ResearchDiscoveryResult) -> SourceDefinition:
        assert result.provider == self.name
        return self.source_definition

    def search(self, query: ResearchQuery) -> ResearchDiscoveryBatch:
        raise AssertionError(query)

    def fetch(self, result: ResearchDiscoveryResult) -> RawArtifact:
        raise AssertionError(result)

    def search_as_of(
        self,
        query: ResearchQuery,
        *,
        requested_as_of: datetime,
    ) -> ResearchDiscoveryBatch:
        self.search_cutoffs.append(requested_as_of)
        return ResearchDiscoveryBatch(
            query=query,
            results=(
                ResearchDiscoveryResult(
                    result_id="archived-result",
                    provider=self.name,
                    canonical_uri="https://archive.example.test/result",
                    discovered_at=_DECISION_AT,
                    available_at=_CUTOFF,
                    published_at=_CUTOFF,
                    provenance_group="archive",
                ),
            ),
            searched_at=_DECISION_AT,
        )

    def fetch_as_of(
        self,
        result: ResearchDiscoveryResult,
        *,
        requested_as_of: datetime,
    ) -> RawArtifact:
        self.fetch_cutoffs.append(requested_as_of)
        content = b"certified historical evidence"
        digest = hashlib.sha256(content).hexdigest()
        item = SourceItem(
            source_item_id="research.certified:item",
            source_id=self.source_definition.source_id,
            source_definition_hash=source_definition_hash(self.source_definition),
            canonical_uri=result.canonical_uri,
            discovered_at=_DECISION_AT,
            content_version=digest,
        )
        return RawArtifact(
            source_item=item,
            content=content,
            media_type="text/plain",
            retrieved_at=_DECISION_AT,
            canonical_uri=result.canonical_uri,
            content_hash=digest,
        )


class _MediaProvider(_CertifiedProvider):
    name: str = "media"
    source_definition: SourceDefinition = _CertifiedProvider.source_definition.model_copy(
        update={"source_id": "research.media"},
    )

    @override
    def fetch(self, result: ResearchDiscoveryResult) -> RawArtifact:
        digest = hashlib.sha256(_VIDEO).hexdigest()
        item = SourceItem(
            source_item_id="research.media:item",
            source_id=self.source_definition.source_id,
            source_definition_hash=source_definition_hash(self.source_definition),
            canonical_uri=result.canonical_uri,
            discovered_at=_DECISION_AT,
            content_version=digest,
        )
        return RawArtifact(
            source_item=item,
            content=_VIDEO,
            media_type="video/mp4",
            retrieved_at=_DECISION_AT,
            canonical_uri=result.canonical_uri,
            content_hash=digest,
            filename=Path("video.mp4"),
        )

    @override
    def search(self, query: ResearchQuery) -> ResearchDiscoveryBatch:
        return ResearchDiscoveryBatch(
            query=query,
            results=(
                ResearchDiscoveryResult(
                    result_id="media-result",
                    provider=self.name,
                    canonical_uri="https://archive.example.test/video",
                    discovered_at=_DECISION_AT,
                    provenance_group="archive",
                ),
            ),
            searched_at=_DECISION_AT,
        )


class _MediaAnalyzer:
    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        assert path.read_bytes() == _VIDEO
        assert media_type == "video/mp4"
        return MediaAnalysis(
            transcript=(TimedTranscriptSegment(start_seconds=0, end_seconds=1, text="claim"),),
            frames=(FrameReading(timestamp_seconds=1, on_screen_text=("frame",), image_png=_FRAME),),
        )


def _certified_runtime_work(
    tmp_path: Path,
) -> tuple[ResearchRuntime, ResearchSession, ResearchTask, _CertifiedProvider]:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    _seed_run(database)
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-certified",
        subject="Historical candidate",
        direction=ThesisDirection.LONG,
        instrument_reference="HISTORICAL",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        created_at=_DECISION_AT,
        known_at=_DECISION_AT,
    )
    ThesisRepository(database).append_candidate(candidate)
    provider = _CertifiedProvider()
    providers = ResearchProviderRegistry()
    providers.register(provider)
    runtime = build_research_runtime(
        database,
        providers,
        AssetStore(tmp_path / "assets"),
        claims=_claims(database),
        interpreter=_interpreter(),
        credentials=SecretSpecInferenceResolver(tmp_path / "unused-secretspec.toml"),
        model="test-model",
        work_repository=IntelligenceWorkRepository(database),
    )
    session = ResearchSession(
        session_id="session-certified",
        run_id=_RUN_ID,
        scope=CandidateThesisResearchScope(candidate_thesis_id=candidate.candidate_thesis_id),
        started_at=_DECISION_AT,
        deadline_at=_DECISION_AT + timedelta(minutes=10),
    )
    _ = runtime.service.start(session)
    task = ResearchTask(
        task_id="task-certified",
        session_id=session.session_id,
        round_number=1,
        query=ResearchQuery(
            provider=provider.name,
            query_text="historical fact",
            candidate_thesis_id=candidate.candidate_thesis_id,
            purpose="verify historical premise",
            requested_at=_DECISION_AT,
            max_results=1,
        ),
        created_at=_DECISION_AT,
    )
    return runtime, session, task, provider


@pytest.mark.parametrize("committed_phase", ["record_search", "record_fetch"])
def test_run_round_resumes_after_hard_kill_without_repeating_committed_provider_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed_phase: str,
) -> None:
    runtime, session, task, provider = _certified_runtime_work(tmp_path)
    original_search = runtime.repository.record_search
    original_fetch = runtime.repository.record_fetch
    if committed_phase == "record_search":

        def commit_search_then_kill(
            value: ResearchTask,
            batch: ResearchDiscoveryBatch,
        ) -> int:
            _ = original_search(value, batch)
            raise SystemExit("hard kill after durable search")

        monkeypatch.setattr(runtime.repository, "record_search", commit_search_then_kill)
    else:

        def commit_fetch_then_kill(value: ResearchFetch) -> bool:
            _ = original_fetch(value)
            raise SystemExit("hard kill after durable fetch")

        monkeypatch.setattr(runtime.repository, "record_fetch", commit_fetch_then_kill)
    with pytest.raises(SystemExit):
        _ = runtime.service.run_round(
            session,
            (task,),
            requested_as_of=_CUTOFF,
            decision_at=_DECISION_AT,
            historical_explicit=True,
        )
    monkeypatch.setattr(runtime.repository, "record_search", original_search)
    monkeypatch.setattr(runtime.repository, "record_fetch", original_fetch)

    result = runtime.service.run_round(
        session,
        (task,),
        requested_as_of=_CUTOFF,
        decision_at=_DECISION_AT,
        historical_explicit=True,
    )

    assert (
        provider.search_cutoffs,
        provider.fetch_cutoffs,
        result.asset_ids,
    ) == (
        [_CUTOFF],
        [_CUTOFF],
        (hashlib.sha256(b"certified historical evidence").hexdigest(),),
    )


def test_run_round_blocks_current_only_provider_before_io_for_historical_run(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    _seed_run(database)
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-1",
        subject="Historical candidate",
        direction=ThesisDirection.LONG,
        instrument_reference="HISTORICAL",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        created_at=_DECISION_AT,
        known_at=_DECISION_AT,
    )
    ThesisRepository(database).append_candidate(candidate)
    providers = ResearchProviderRegistry()
    providers.register(_CurrentOnlyProvider())
    runtime = build_research_runtime(
        database,
        providers,
        AssetStore(tmp_path / "assets"),
        claims=_claims(database),
        interpreter=_interpreter(),
        credentials=SecretSpecInferenceResolver(tmp_path / "unused-secretspec.toml"),
        model="test-model",
        work_repository=IntelligenceWorkRepository(database),
    )
    session = ResearchSession(
        session_id="session-1",
        run_id="4fa85f64-5717-4562-b3fc-2c963f66afa6",
        scope=CandidateThesisResearchScope(candidate_thesis_id=candidate.candidate_thesis_id),
        started_at=_DECISION_AT,
        deadline_at=_DECISION_AT + timedelta(minutes=10),
    )
    _ = runtime.service.start(session)
    task = ResearchTask(
        task_id="task-1",
        session_id=session.session_id,
        round_number=1,
        query=ResearchQuery(
            provider="current-web",
            query_text="historical fact",
            candidate_thesis_id=candidate.candidate_thesis_id,
            purpose="verify historical premise",
            requested_at=_DECISION_AT,
            max_results=2,
        ),
        created_at=_DECISION_AT,
    )

    result = runtime.service.run_round(
        session,
        (task,),
        requested_as_of=_CUTOFF,
        decision_at=_DECISION_AT,
        historical_explicit=True,
    )

    assert result.failure_kinds == (HistoricalResearchUnavailableError.__name__,)


def test_run_round_uses_certified_cutoff_search_and_fetch_methods(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    _seed_run(database)
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-certified",
        subject="Historical candidate",
        direction=ThesisDirection.LONG,
        instrument_reference="HISTORICAL",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        created_at=_DECISION_AT,
        known_at=_DECISION_AT,
    )
    ThesisRepository(database).append_candidate(candidate)
    provider = _CertifiedProvider()
    providers = ResearchProviderRegistry()
    providers.register(provider)
    runtime = build_research_runtime(
        database,
        providers,
        AssetStore(tmp_path / "assets"),
        claims=_claims(database),
        interpreter=_interpreter(),
        credentials=SecretSpecInferenceResolver(tmp_path / "unused-secretspec.toml"),
        model="test-model",
        work_repository=IntelligenceWorkRepository(database),
    )
    session = ResearchSession(
        session_id="session-certified",
        run_id="4fa85f64-5717-4562-b3fc-2c963f66afa6",
        scope=CandidateThesisResearchScope(candidate_thesis_id=candidate.candidate_thesis_id),
        started_at=_DECISION_AT,
        deadline_at=_DECISION_AT + timedelta(minutes=10),
    )
    _ = runtime.service.start(session)
    task = ResearchTask(
        task_id="task-certified",
        session_id=session.session_id,
        round_number=1,
        query=ResearchQuery(
            provider=provider.name,
            query_text="historical fact",
            candidate_thesis_id=candidate.candidate_thesis_id,
            purpose="verify historical premise",
            requested_at=_DECISION_AT,
            max_results=1,
        ),
        created_at=_DECISION_AT,
    )

    result = runtime.service.run_round(
        session,
        (task,),
        requested_as_of=_CUTOFF,
        decision_at=_DECISION_AT,
        historical_explicit=True,
    )

    assert provider.search_cutoffs == [_CUTOFF]
    assert provider.fetch_cutoffs == [_CUTOFF]
    assert result.asset_ids == (hashlib.sha256(b"certified historical evidence").hexdigest(),)


def test_run_round_returns_primary_and_derived_asset_ids_with_per_asset_attempts(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    _seed_run(database)
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-media",
        subject="Media candidate",
        direction=ThesisDirection.LONG,
        instrument_reference="MEDIA",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        created_at=_DECISION_AT,
        known_at=_DECISION_AT,
    )
    ThesisRepository(database).append_candidate(candidate)
    provider = _MediaProvider()
    providers = ResearchProviderRegistry()
    providers.register(provider)
    repository = ResearchRepository(database)
    processors = EvidenceProcessorRegistry()
    processors.register(MediaEvidenceProcessor(_MediaAnalyzer()))
    service = ResearchService(
        providers,
        repository,
        SourceRepository(database),
        EvidenceRepository(database),
        processors,
        EvidenceProcessingAttemptRepository(database),
        AssetStore(tmp_path / "assets"),
        clock=lambda: _DECISION_AT,
    )
    session = ResearchSession(
        session_id="session-media",
        run_id="4fa85f64-5717-4562-b3fc-2c963f66afa6",
        scope=CandidateThesisResearchScope(candidate_thesis_id=candidate.candidate_thesis_id),
        started_at=_DECISION_AT,
        deadline_at=_DECISION_AT + timedelta(minutes=10),
    )
    _ = service.start(session)
    task = ResearchTask(
        task_id="task-media",
        session_id=session.session_id,
        round_number=1,
        query=ResearchQuery(
            provider=provider.name,
            query_text="media fact",
            candidate_thesis_id=candidate.candidate_thesis_id,
            purpose="verify",
            requested_at=_DECISION_AT,
            max_results=1,
        ),
        created_at=_DECISION_AT,
    )

    result = service.run_round(session, (task,), decision_at=_DECISION_AT)

    primary_digest = hashlib.sha256(_VIDEO).hexdigest()
    frame_digest = hashlib.sha256(_FRAME).hexdigest()
    assert result.asset_ids == (primary_digest, frame_digest)
    with database.transaction() as connection:
        rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                "SELECT asset_id FROM evidence_processing_attempts",
            ).fetchall(),
        )
        attempt_assets = {str(cast("object", row["asset_id"])) for row in rows}
    assert attempt_assets == {primary_digest, frame_digest}
