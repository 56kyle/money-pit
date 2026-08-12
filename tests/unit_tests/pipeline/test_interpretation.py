from datetime import UTC
from datetime import datetime
from pathlib import Path

from typing_extensions import override

from money_pit.contracts import ClaimObservationDraft
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import InterpretationRequest
from money_pit.evidence.work import EvidenceInterpretationWork
from money_pit.pipeline.interpretation import interpret_document
from money_pit.pipeline.interpretation import make_interpretation_node
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
        del as_of, source_id, limit, interpreter_version
        return self.works

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
    ) -> None:
        del completed_at, known_at
        self.records.append((admissions, artifact))


def _zero_claim_agent(request: InterpretationRequest) -> InterpretationDraft:
    assert request.evidence
    return InterpretationDraft(observations=())


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

    def fallback_agent(request: InterpretationRequest) -> InterpretationDraft:
        return InterpretationDraft(
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

    observations = interpret_document(
        document,
        agent=fallback_agent,
        requested_as_of=requested_as_of,
        decision_at=context_known_at,
        known_at=context_known_at,
        character_budget=10_000,
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
