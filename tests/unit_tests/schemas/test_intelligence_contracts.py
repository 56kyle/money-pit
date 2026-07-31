from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TimestampLocator
from money_pit.schemas.sources import SourceItem


NOW = datetime(2026, 7, 29, tzinfo=UTC)
DIGEST = "a" * 64


def test_evidence_document_preserves_timestamp_and_frame_provenance():
    asset = EvidenceAsset(
        asset_id=DIGEST,
        content_hash=DIGEST,
        media_type="image/jpeg",
        source_item_id="video:item",
        local_path=Path("aa") / DIGEST,
        retrieved_at=NOW,
    )
    fragment = EvidenceFragment(
        fragment_id="frame-12.5",
        asset_id=asset.asset_id,
        kind="frame",
        locator=TimestampLocator(
            start_seconds=12.5,
            end_seconds=13.0,
            bounding_box=(0.1, 0.2, 0.8, 0.9),
        ),
        cited_source_text="Source: SEC filing",
        extraction_method="vision-model",
        extraction_model="model-v1",
        confidence=0.91,
    )

    document = EvidenceDocument(asset=asset, fragments=(fragment,))

    assert document.fragments[0] == fragment


def test_claim_observation_preserves_temporal_supersession_and_evidence_links():
    observation = ClaimObservation(
        observation_id="observation-2",
        canonical_claim_key="issuer:revenue-growth",
        claim_text="Revenue growth slowed.",
        claim_kind=ClaimKind.FACTUAL,
        subjects=("Issuer",),
        instruments=("ISSR",),
        source_item_id="video:item",
        evidence_fragment_ids=("transcript-30", "frame-31"),
        asserted_at=NOW,
        recorded_at=NOW,
        valid_from=NOW,
        horizon="quarter",
        expires_at=datetime(2026, 10, 29, tzinfo=UTC),
        supersedes_observation_id="observation-1",
    )

    assert observation.evidence_fragment_ids == ("transcript-30", "frame-31")
    assert observation.supersedes_observation_id == "observation-1"
    assert observation.expires_at == datetime(2026, 10, 29, tzinfo=UTC)


def test_claim_observation_rejects_naive_recorded_time():
    with pytest.raises(ValidationError):
        ClaimObservation(
            observation_id="observation",
            canonical_claim_key="claim",
            claim_text="Revenue grew.",
            claim_kind=ClaimKind.FACTUAL,
            source_item_id="source:item",
            asserted_at=NOW,
            recorded_at=NOW.replace(tzinfo=None),
        )


def test_verification_result_rejects_naive_recorded_time():
    with pytest.raises(ValidationError):
        VerificationResult(
            verification_id="verification",
            observation_id="observation",
            status=VerificationStatus.SUPPORTED,
            checked_at=NOW,
            recorded_at=NOW.replace(tzinfo=None),
            verifier_version="v1",
        )


def test_source_item_rejects_naive_discovery_time():
    with pytest.raises(ValidationError):
        SourceItem(
            source_item_id="item",
            source_id="source",
            canonical_uri="https://example.com",
            discovered_at=NOW.replace(tzinfo=None),
            content_version="v1",
        )


def test_evidence_fragment_rejects_invalid_confidence():
    with pytest.raises(ValidationError):
        EvidenceFragment(
            fragment_id="fragment",
            asset_id="asset",
            kind="frame",
            locator=TimestampLocator(start_seconds=0),
            extraction_method="model",
            confidence=1.1,
        )
