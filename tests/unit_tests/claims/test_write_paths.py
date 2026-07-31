"""Tests for typed immutable claim write paths."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from datetime import datetime
from pathlib import Path
from threading import Barrier

import pytest

from money_pit.claims.repository import ClaimEvidenceFragmentNotFoundError
from money_pit.claims.repository import ClaimEvidenceProvenanceError
from money_pit.claims.repository import ClaimObservationNotFoundError
from money_pit.claims.repository import ClaimRepository
from money_pit.claims.repository import ClaimSourceItemNotFoundError
from money_pit.claims.repository import ImmutableClaimCollisionError
from money_pit.claims.repository import VerificationEvidenceNotFoundError
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.sources.service import EvidenceRepository
from money_pit.storage.database import Database
from money_pit.storage.sources import SourceRepository


_NOW = datetime(2026, 7, 29, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def source_item() -> SourceItem:
    return SourceItem(
        source_item_id="source:item",
        source_id="source",
        canonical_uri="https://example.com/source-item",
        discovered_at=_NOW,
        content_version="source-version",
    )


@pytest.fixture
def evidence_document(source_item: SourceItem) -> EvidenceDocument:
    digest = "a" * 64
    return EvidenceDocument(
        asset=EvidenceAsset(
            asset_id=digest,
            content_hash=digest,
            media_type="text/plain",
            source_item_id=source_item.source_item_id,
            local_path=Path("aa") / digest,
            retrieved_at=_NOW,
        ),
        fragments=(
            EvidenceFragment(
                fragment_id="fragment-1",
                asset_id=digest,
                kind="web_span",
                locator=TextLocator(start_offset=0, end_offset=7),
                extracted_text="Revenue",
                extraction_method="test",
            ),
            EvidenceFragment(
                fragment_id="fragment-2",
                asset_id=digest,
                kind="web_span",
                locator=TextLocator(start_offset=8, end_offset=12),
                extracted_text="grew",
                extraction_method="test",
            ),
        ),
    )


@pytest.fixture
def repository(
    database: Database,
    source_item: SourceItem,
    evidence_document: EvidenceDocument,
) -> ClaimRepository:
    source_repository = SourceRepository(database)
    source_repository.register_definition(
        SourceDefinition(
            source_id=source_item.source_id,
            adapter_name="test",
            locator=source_item.canonical_uri,
        ),
        registry_version=1,
        registered_at=_NOW,
    )
    source_repository.persist_discovery(
        source_item.source_id,
        (source_item,),
        next_cursor=None,
        updated_at=_NOW,
    )
    EvidenceRepository(database).persist(evidence_document, source_item=source_item)
    return ClaimRepository(database)


@pytest.fixture
def observation() -> ClaimObservation:
    return ClaimObservation(
        observation_id="observation-1",
        canonical_claim_key="revenue-growth",
        claim_text="Revenue grew.",
        claim_kind=ClaimKind.FACTUAL,
        subjects=("Acme",),
        instruments=("ACME",),
        source_item_id="source:item",
        evidence_fragment_ids=("fragment-1",),
        asserted_at=_NOW,
        recorded_at=_NOW,
    )


@pytest.fixture
def verification(observation: ClaimObservation) -> VerificationResult:
    return VerificationResult(
        verification_id="verification-1",
        observation_id=observation.observation_id,
        status=VerificationStatus.SUPPORTED,
        supporting_evidence_ids=("fragment-2",),
        checked_at=_NOW,
        recorded_at=_NOW,
        verifier_version="verifier-v1",
    )


def test_append_observation_persists_typed_history(
    repository: ClaimRepository,
    observation: ClaimObservation,
) -> None:
    repository.append_observation(observation)

    observations, verifications = repository.history(observation.canonical_claim_key)

    assert observations == (observation,)
    assert verifications == ()


def test_append_observation_with_identical_content_is_idempotent(
    repository: ClaimRepository,
    observation: ClaimObservation,
) -> None:
    repository.append_observation(observation)
    repository.append_observation(observation)

    observations, _ = repository.history(observation.canonical_claim_key)

    assert observations == (observation,)


def test_append_observation_with_reused_id_and_different_content_fails_closed(
    repository: ClaimRepository,
    observation: ClaimObservation,
) -> None:
    repository.append_observation(observation)
    collision: ClaimObservation = observation.model_copy(update={"claim_text": "Revenue fell."})

    with pytest.raises(ImmutableClaimCollisionError):
        repository.append_observation(collision)


def test_append_verification_persists_typed_history_idempotently(
    repository: ClaimRepository,
    observation: ClaimObservation,
    verification: VerificationResult,
) -> None:
    repository.append_observation(observation)
    repository.append_verification(verification)
    repository.append_verification(verification)

    _, verifications = repository.history(observation.canonical_claim_key)

    assert verifications == (verification,)


def test_append_verification_with_reused_id_and_different_content_fails_closed(
    repository: ClaimRepository,
    observation: ClaimObservation,
    verification: VerificationResult,
) -> None:
    repository.append_observation(observation)
    repository.append_verification(verification)
    collision: VerificationResult = verification.model_copy(update={"status": VerificationStatus.CONTRADICTED})

    with pytest.raises(ImmutableClaimCollisionError):
        repository.append_verification(collision)


def test_append_verification_with_unknown_observation_fails_explicitly(
    repository: ClaimRepository,
    verification: VerificationResult,
) -> None:
    with pytest.raises(ClaimObservationNotFoundError):
        repository.append_verification(verification)


def test_append_observation_with_simultaneous_identical_content_is_idempotent(
    repository: ClaimRepository,
    observation: ClaimObservation,
) -> None:
    barrier = Barrier(2)

    def append() -> None:
        barrier.wait()
        repository.append_observation(observation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: append(), range(2)))

    observations, _ = repository.history(observation.canonical_claim_key)
    assert results == (None, None)
    assert observations == (observation,)


def test_append_observation_with_missing_source_fails_explicitly(
    repository: ClaimRepository,
    observation: ClaimObservation,
) -> None:
    missing_source = observation.model_copy(update={"source_item_id": "missing:item"})

    with pytest.raises(ClaimSourceItemNotFoundError):
        repository.append_observation(missing_source)


def test_append_observation_with_missing_fragment_fails_explicitly(
    repository: ClaimRepository,
    observation: ClaimObservation,
) -> None:
    missing_fragment = observation.model_copy(
        update={"evidence_fragment_ids": ("missing-fragment",)},
    )

    with pytest.raises(ClaimEvidenceFragmentNotFoundError):
        repository.append_observation(missing_fragment)


def test_append_observation_requires_fragment_acquired_from_its_source(
    repository: ClaimRepository,
    database: Database,
    observation: ClaimObservation,
) -> None:
    other_item = SourceItem(
        source_item_id="other:item",
        source_id="other",
        canonical_uri="https://example.com/other-item",
        discovered_at=_NOW,
        content_version="other-version",
    )
    source_repository = SourceRepository(database)
    source_repository.register_definition(
        SourceDefinition(
            source_id=other_item.source_id,
            adapter_name="test",
            locator=other_item.canonical_uri,
        ),
        registry_version=1,
        registered_at=_NOW,
    )
    source_repository.persist_discovery(
        other_item.source_id,
        (other_item,),
        next_cursor=None,
        updated_at=_NOW,
    )
    digest = "b" * 64
    other_document = EvidenceDocument(
        asset=EvidenceAsset(
            asset_id=digest,
            content_hash=digest,
            media_type="text/plain",
            source_item_id=other_item.source_item_id,
            local_path=Path("bb") / digest,
            retrieved_at=_NOW,
        ),
        fragments=(
            EvidenceFragment(
                fragment_id="other-fragment",
                asset_id=digest,
                kind="web_span",
                locator=TextLocator(start_offset=0, end_offset=5),
                extracted_text="other",
                extraction_method="test",
            ),
        ),
    )
    EvidenceRepository(database).persist(other_document, source_item=other_item)
    wrong_provenance = observation.model_copy(
        update={"evidence_fragment_ids": ("other-fragment",)},
    )

    with pytest.raises(ClaimEvidenceProvenanceError):
        repository.append_observation(wrong_provenance)


@pytest.mark.parametrize(
    "evidence_field",
    ["supporting_evidence_ids", "contradicting_evidence_ids"],
)
def test_append_verification_with_missing_evidence_fails_explicitly(
    repository: ClaimRepository,
    observation: ClaimObservation,
    verification: VerificationResult,
    evidence_field: str,
) -> None:
    repository.append_observation(observation)
    missing_evidence = verification.model_copy(
        update={
            "supporting_evidence_ids": (),
            evidence_field: ("missing-fragment",),
        },
    )

    with pytest.raises(VerificationEvidenceNotFoundError):
        repository.append_verification(missing_evidence)


def test_append_observation_allows_empty_evidence(
    repository: ClaimRepository,
    observation: ClaimObservation,
) -> None:
    without_evidence = observation.model_copy(update={"evidence_fragment_ids": ()})

    repository.append_observation(without_evidence)

    observations, _ = repository.history(observation.canonical_claim_key)
    assert observations == (without_evidence,)


def test_append_verification_allows_empty_evidence(
    repository: ClaimRepository,
    observation: ClaimObservation,
    verification: VerificationResult,
) -> None:
    repository.append_observation(observation)
    without_evidence = verification.model_copy(
        update={
            "supporting_evidence_ids": (),
            "contradicting_evidence_ids": (),
        },
    )

    repository.append_verification(without_evidence)

    _, verifications = repository.history(observation.canonical_claim_key)
    assert verifications == (without_evidence,)
