from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import cast

from typing_extensions import override

from money_pit.agents.inference import InferenceInvocationContext
from money_pit.agents.inference import InferenceResult
from money_pit.agents.inference import InferenceUsage
from money_pit.contracts import ClaimObservationDraft
from money_pit.contracts import EvidencePromptRecord
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import InterpretationRequest
from money_pit.evidence.aliases import AliasedEvidence
from money_pit.evidence.aliases import EvidenceAliasProjection
from money_pit.evidence.work import EvidenceInterpretationWork
from money_pit.evidence.work import ReusableInterpretation
from money_pit.pipeline.interpretation import interpret_document
from money_pit.pipeline.interpretation import make_interpretation_node
from money_pit.pipeline.interpretation import materialize_observation
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import StageArtifactRecord
from money_pit.schemas.runs import bind_artifact_record
from money_pit.storage.admission import IntelligenceAdmissionRepository
from money_pit.storage.admission import InterpretationAdmission
from money_pit.storage.database import Database
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import InterpretationBundleRecord
from money_pit.storage.intelligence_work import InterpretationChunkRecord
from money_pit.storage.intelligence_work import InterpretationChunkSpec
from money_pit.storage.intelligence_work import WorkStatus


class _EvidenceMemory:
    def __init__(
        self,
        work: EvidenceInterpretationWork | tuple[EvidenceInterpretationWork, ...],
    ) -> None:
        self.works: tuple[EvidenceInterpretationWork, ...] = work if isinstance(work, tuple) else (work,)
        self.attempt_ids: dict[int, str] = {
            id(item): f"attempt-{index}" for index, item in enumerate(self.works, start=1)
        }
        self.completed_observation_ids: list[tuple[str, ...]] = []

    def list_pending_documents(
        self,
        *,
        as_of: datetime,
        source_id: str | None,
        limit: int,
        interpreter_version: str,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        del as_of, source_id, interpreter_version
        return self.works[:limit]

    def documents_for_bundle(
        self,
        *,
        source_item_id: str,
        content_version: str,
        processor_name: str | None = None,
        processor_version: str | None = None,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        del processor_name, processor_version
        return tuple(
            item
            for item in self.works
            if item.document.asset.source_item_id == source_item_id and item.content_version == content_version
        )

    def reusable_interpretation(
        self,
        work: EvidenceInterpretationWork,
        *,
        interpreter_version: str,
    ) -> ReusableInterpretation | None:
        del work, interpreter_version
        return None

    def preserved_legacy_interpretation(
        self,
        work: EvidenceInterpretationWork,
    ) -> ReusableInterpretation | None:
        del work
        return None

    def begin_interpretation(
        self,
        work: EvidenceInterpretationWork,
        *,
        run_id: str,
        interpreter_version: str,
        started_at: datetime,
    ) -> str:
        del run_id, interpreter_version, started_at
        return self.attempt_ids[id(work)]

    def complete_interpretation(
        self,
        attempt_id: str,
        *,
        observation_ids: tuple[str, ...],
        known_at: datetime,
        completed_at: datetime,
    ) -> None:
        del attempt_id, known_at, completed_at
        self.completed_observation_ids.append(observation_ids)

    def fail_interpretation(
        self,
        attempt_id: str,
        *,
        failure_kind: str,
        completed_at: datetime,
    ) -> None:
        raise AssertionError((attempt_id, failure_kind, completed_at))


class _AdmissionMemory(IntelligenceAdmissionRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.records: list[tuple[tuple[InterpretationAdmission, ...], StageArtifactRecord]] = []

    @override
    def admit_interpretation(
        self,
        admissions: tuple[InterpretationAdmission, ...],
        *,
        completed_at: datetime,
        known_at: datetime,
        artifact: StageArtifactRecord,
        bundle_id: str | None = None,
    ) -> None:
        del completed_at, known_at, bundle_id
        self.records.append((admissions, artifact))


class _CompletedBundleEvidenceMemory(_EvidenceMemory):
    @override
    def list_pending_documents(
        self,
        *,
        as_of: datetime,
        source_id: str | None,
        limit: int,
        interpreter_version: str,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        del as_of, source_id, limit, interpreter_version
        return ()


class _BundleWorkMemory:
    def __init__(self) -> None:
        self.bundle: InterpretationBundleRecord | None = None
        self.chunks: tuple[InterpretationChunkSpec, ...] = ()

    def ensure_interpretation_bundle(
        self,
        bundle: InterpretationBundleRecord,
        chunks: tuple[InterpretationChunkSpec, ...],
    ) -> None:
        self.bundle = bundle
        self.chunks = chunks

    def claim_interpretation_chunk(self, **_kwargs: object) -> InterpretationChunkRecord | None:
        return None

    def ready_interpretation_bundle(self, source_id: str | None) -> str | None:
        del source_id
        return None


class _CompletedBundleWorkMemory:
    def __init__(self, chunk: InterpretationChunkRecord) -> None:
        self.chunk: InterpretationChunkRecord = chunk

    def claim_interpretation_chunk(self, **_kwargs: object) -> None:
        return None

    def ready_interpretation_bundle(self, source_id: str | None) -> str:
        del source_id
        return self.chunk.bundle_id

    def interpretation_chunks_for_bundle(self, bundle_id: str) -> tuple[InterpretationChunkRecord, ...]:
        assert bundle_id == self.chunk.bundle_id
        return (self.chunk,)

    def source_id_for_bundle(self, bundle_id: str) -> str:
        assert bundle_id == self.chunk.bundle_id
        return "manual"

    def append_discovery_unit(self, *_args: object) -> None:
        return None


def _result(output: InterpretationDraft) -> InferenceResult[InterpretationDraft]:
    return InferenceResult(output=output, usage=InferenceUsage(), request_hash="0" * 64)


def _zero_claim_agent(
    request: InterpretationRequest, *, context: InferenceInvocationContext
) -> InferenceResult[InterpretationDraft]:
    del context
    assert request.evidence
    return _result(InterpretationDraft(observations=()))


def test_interpret_document_materializes_fallback_assertion_at_requested_boundary() -> None:
    requested_as_of = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    context_known_at = datetime(2026, 8, 12, 15, 30, 0, 99_000, tzinfo=UTC)
    asset_id = "a" * 64
    document = EvidenceDocument(
        asset=EvidenceAsset(
            asset_id=asset_id,
            content_hash=asset_id,
            media_type="text/plain",
            source_item_id="manual:item",
            local_path=Path("aa") / asset_id,
            retrieved_at=requested_as_of,
        ),
        fragments=(
            EvidenceFragment(
                fragment_id="fragment-1",
                asset_id=asset_id,
                kind="web_span",
                locator=TextLocator(start_offset=0, end_offset=18),
                extracted_text="A supported claim.",
                extraction_method="test",
            ),
        ),
    )

    def fallback_agent(
        request: InterpretationRequest, *, context: InferenceInvocationContext
    ) -> InferenceResult[InterpretationDraft]:
        del context
        return _result(
            InterpretationDraft(
                observations=(
                    ClaimObservationDraft(
                        claim_text="A supported claim.",
                        claim_kind=ClaimKind.FACTUAL,
                        category=ClaimCategory.MARKET,
                        evidence_aliases=(request.evidence[0].alias,),
                        asserted_at=request.requested_as_of,
                        horizon_class=HorizonClass.TACTICAL,
                    ),
                ),
            )
        )

    observations = interpret_document(
        document,
        agent=fallback_agent,
        requested_as_of=requested_as_of,
        decision_at=context_known_at,
        known_at=context_known_at,
        character_budget=10_000,
        context=InferenceInvocationContext(run_id="run-1", work_unit_id="document-1"),
        baseline_only=True,
    )

    assert observations[0].asserted_at == requested_as_of


def test_make_interpretation_node_records_success_for_a_zero_claim_document(tmp_path: Path) -> None:
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "a" * 64
    work = EvidenceInterpretationWork(
        content_version="sha256:version",
        document=EvidenceDocument(
            asset=EvidenceAsset(
                asset_id=asset_id,
                content_hash=asset_id,
                media_type="text/plain",
                source_item_id="manual:item",
                local_path=Path("aa") / asset_id,
                retrieved_at=as_of,
            ),
            fragments=(
                EvidenceFragment(
                    fragment_id="fragment-1",
                    asset_id=asset_id,
                    kind="web_span",
                    locator=TextLocator(start_offset=0, end_offset=16),
                    extracted_text="No material claim",
                    extraction_method="test",
                ),
            ),
        ),
    )
    evidence = _EvidenceMemory(work)
    admission = _AdmissionMemory(Database(tmp_path / "unused-admission.sqlite3"))
    node = make_interpretation_node(
        evidence=evidence,
        admission=admission,
        agent=_zero_claim_agent,
        implementation_version="interpretation-v1",
        clock=lambda: as_of,
    )

    _ = node(
        {
            "run_id": "run-1",
            "run_dir": str(tmp_path / "runs" / "run-1"),
            "requested_as_of": as_of,
            "run_started_at": as_of,
        },
    )

    admissions, artifact = admission.records[0]
    assert (admissions, artifact.input_ids, artifact.output_ids) == (
        (InterpretationAdmission(attempt_id="attempt-1"),),
        (
            bind_artifact_record(
                ArtifactRecordKind.SOURCE_ITEM_VERSION,
                "manual:item@sha256:version",
            ),
            bind_artifact_record(ArtifactRecordKind.ASSET, asset_id),
            bind_artifact_record(ArtifactRecordKind.DOCUMENT, asset_id),
            bind_artifact_record(ArtifactRecordKind.FRAGMENT, "fragment-1"),
        ),
        (bind_artifact_record(ArtifactRecordKind.INTERPRETATION_ATTEMPT, "attempt-1"),),
    )


def test_make_interpretation_node_deduplicates_shared_asset_and_fragment_inputs(tmp_path: Path) -> None:
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "a" * 64
    document = EvidenceDocument(
        asset=EvidenceAsset(
            asset_id=asset_id,
            content_hash=asset_id,
            media_type="text/plain",
            source_item_id="manual:item",
            local_path=Path("aa") / asset_id,
            retrieved_at=as_of,
        ),
        fragments=(
            EvidenceFragment(
                fragment_id="fragment-shared",
                asset_id=asset_id,
                kind="web_span",
                locator=TextLocator(start_offset=0, end_offset=16),
                extracted_text="No material claim",
                extraction_method="test",
            ),
        ),
    )
    evidence = _EvidenceMemory(
        (
            EvidenceInterpretationWork(content_version="sha256:version-1", document=document),
            EvidenceInterpretationWork(content_version="sha256:version-2", document=document),
        )
    )
    admission = _AdmissionMemory(Database(tmp_path / "unused-admission.sqlite3"))
    node = make_interpretation_node(
        evidence=evidence,
        admission=admission,
        agent=_zero_claim_agent,
        implementation_version="interpretation-v1",
        clock=lambda: as_of,
    )

    _ = node(
        {
            "run_id": "run-1",
            "run_dir": str(tmp_path / "runs" / "run-1"),
            "requested_as_of": as_of,
            "run_started_at": as_of,
        },
    )

    artifact = admission.records[0][1]
    assert artifact.input_ids.count(bind_artifact_record(ArtifactRecordKind.ASSET, asset_id)) == 1
    assert artifact.input_ids.count(bind_artifact_record(ArtifactRecordKind.DOCUMENT, asset_id)) == 1
    assert artifact.input_ids.count(bind_artifact_record(ArtifactRecordKind.FRAGMENT, "fragment-shared")) == 1


def test_make_interpretation_node_bundles_complete_source_version_beyond_document_limit(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    works = tuple(
        EvidenceInterpretationWork(
            content_version="version-1",
            document=EvidenceDocument(
                asset=EvidenceAsset(
                    asset_id=str(index) * 64,
                    content_hash=str(index) * 64,
                    media_type="text/plain",
                    source_item_id="video:item",
                    local_path=Path(str(index) * 2) / (str(index) * 64),
                    retrieved_at=as_of,
                ),
                fragments=(
                    EvidenceFragment(
                        fragment_id=f"fragment-{index}",
                        asset_id=str(index) * 64,
                        kind="web_span",
                        locator=TextLocator(start_offset=0, end_offset=8),
                        extracted_text=f"claim {index}",
                        extraction_method="test",
                    ),
                ),
            ),
        )
        for index in range(1, 4)
    )
    evidence = _EvidenceMemory(works)
    bundle_work = _BundleWorkMemory()
    node = make_interpretation_node(
        evidence=evidence,
        admission=_AdmissionMemory(Database(tmp_path / "unused-admission.sqlite3")),
        agent=_zero_claim_agent,
        implementation_version="interpretation-v2",
        document_limit=1,
        clock=lambda: as_of,
        work_repository=cast("IntelligenceWorkRepository", cast("object", bundle_work)),
    )

    _ = node(
        {
            "run_id": "run-1",
            "run_dir": str(tmp_path / "runs" / "run-1"),
            "requested_as_of": as_of,
            "run_started_at": as_of,
        }
    )

    assert bundle_work.bundle is not None
    assert bundle_work.bundle.payload == {"asset_ids": [str(index) * 64 for index in range(1, 4)]}
    fragment_ids = {
        fragment_id
        for chunk in bundle_work.chunks
        if isinstance(chunk.payload, dict)
        for fragment_ids in cast("dict[str, list[str]]", chunk.payload["alias_map"]).values()
        for fragment_id in fragment_ids
    }
    assert fragment_ids == {"fragment-1", "fragment-2", "fragment-3"}


def test_make_interpretation_node_binds_cross_asset_observation_once(tmp_path: Path) -> None:
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    asset_ids = ("a" * 64, "b" * 64)
    fragment_ids = tuple(f"{asset_id}:fragment" for asset_id in asset_ids)
    works = tuple(
        EvidenceInterpretationWork(
            content_version="version-1",
            document=EvidenceDocument(
                asset=EvidenceAsset(
                    asset_id=asset_id,
                    content_hash=asset_id,
                    media_type="text/plain",
                    source_item_id="video:item",
                    local_path=Path(asset_id[:2]) / asset_id,
                    retrieved_at=as_of,
                ),
                fragments=(
                    EvidenceFragment(
                        fragment_id=fragment_id,
                        asset_id=asset_id,
                        kind="web_span",
                        locator=TextLocator(start_offset=0, end_offset=8),
                        extracted_text="evidence",
                        extraction_method="test",
                    ),
                ),
            ),
        )
        for asset_id, fragment_id in zip(asset_ids, fragment_ids, strict=True)
    )
    evidence = _CompletedBundleEvidenceMemory(works)
    request = InterpretationRequest(
        source_item_id="video:item",
        evidence=tuple(
            EvidencePromptRecord(alias=f"E{index:06d}", kind="web_span", text="evidence", core=True)
            for index in range(1, 3)
        ),
        requested_as_of=as_of,
        context_known_at=as_of,
    )
    draft = ClaimObservationDraft(
        claim_text="The transcript and frame support the same claim.",
        claim_kind=ClaimKind.FACTUAL,
        category=ClaimCategory.MARKET,
        evidence_aliases=("E000001", "E000002"),
        asserted_at=as_of,
        horizon_class=HorizonClass.TACTICAL,
    )
    projection = EvidenceAliasProjection(
        evidence=tuple(
            AliasedEvidence(
                alias=f"E{index:06d}",
                kind="web_span",
                text="evidence",
                fragment_ids=(fragment_id,),
            )
            for index, fragment_id in enumerate(fragment_ids, start=1)
        )
    )
    observation = materialize_observation(
        draft,
        source_item_id="video:item",
        projection=projection,
        known_at=as_of,
        assertion_boundary=as_of,
    )
    chunk = InterpretationChunkRecord(
        chunk_id="chunk-1",
        chunk_number=0,
        input_fingerprint="0" * 64,
        payload={"source_item_id": "video:item", "content_version": "version-1"},
        bundle_id="bundle-1",
        status=WorkStatus.COMPLETED,
        completed_at=as_of,
        output={
            "request": request.model_dump(mode="json"),
            "response": InterpretationDraft(observations=(draft,)).model_dump(mode="json"),
            "observations": [observation.model_dump(mode="json")],
            "alias_map": {"E000001": [fragment_ids[0]], "E000002": [fragment_ids[1]]},
        },
    )
    work = _CompletedBundleWorkMemory(chunk)
    admission = _AdmissionMemory(Database(tmp_path / "unused-admission.sqlite3"))
    node = make_interpretation_node(
        evidence=evidence,
        admission=admission,
        agent=_zero_claim_agent,
        implementation_version="interpretation-v2",
        clock=lambda: as_of,
        work_repository=cast("IntelligenceWorkRepository", cast("object", work)),
    )

    _ = node(
        {
            "run_id": "run-1",
            "run_dir": str(tmp_path / "runs" / "run-1"),
            "requested_as_of": as_of,
            "run_started_at": as_of,
        }
    )

    admissions, artifact = admission.records[0]
    observation_binding = bind_artifact_record(ArtifactRecordKind.OBSERVATION, observation.observation_id)
    assert artifact.output_ids.count(observation_binding) == 1
    assert tuple(item.observations for item in admissions) == ((observation,), (observation,))
