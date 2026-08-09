from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from money_pit.claims.projection import ClaimFreshnessRule
from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.repository import ClaimRepository
from money_pit.claims.repository import ClaimVerificationPolicyError
from money_pit.claims.repository import deterministic_claim_key
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimResolutionDecision
from money_pit.schemas.claims import ClaimResolutionKind
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.sources import SourceRepository


_NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _claim_repository(database: Database) -> ClaimRepository:
    rule = ClaimFreshnessRule(
        review_interval=timedelta(days=7),
        freshness_interval=timedelta(days=30),
    )
    return ClaimRepository(
        database,
        refresh_policy=ClaimRefreshPolicy(
            policy_version="policy-1",
            rules={category: dict.fromkeys(HorizonClass, rule) for category in ClaimCategory},
        ),
    )


def _register_evidence(
    database: Database,
    *,
    source_id: str,
    provenance_group: str,
    allowed_use: AllowedUse,
    trust_level: TrustLevel,
    item_id: str,
    fragment_id: str,
    asset_id: str,
) -> None:
    definition = SourceDefinition(
        source_id=source_id,
        adapter_name="manual",
        locator=f"manual:{source_id}",
        provenance_group=provenance_group,
        allowed_uses=(allowed_use,),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=trust_level),),
    )
    sources = SourceRepository(database)
    _ = sources.register_definition(definition, registry_version="0.0.2", registered_at=_NOW)
    definition_hash = sources.definition_hash(definition)
    _ = sources.persist_discovery(
        source_id,
        (
            SourceItem(
                source_item_id=item_id,
                source_id=source_id,
                source_definition_hash=definition_hash,
                canonical_uri=f"manual:{item_id}",
                discovered_at=_NOW,
                content_version="content-1",
            ),
        ),
        next_cursor=None,
        updated_at=_NOW,
    )
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """
            INSERT INTO evidence_assets (asset_id, content_hash, local_path, metadata_json)
            VALUES (?, ?, ?, '{}')
            """,
            (asset_id, asset_id, f"assets/{asset_id}"),
        )
        _ = connection.execute(
            """
            INSERT INTO evidence_asset_acquisitions (
                acquisition_id, asset_id, source_item_id, content_version,
                source_definition_hash, retrieved_at, media_type
            ) VALUES (?, ?, ?, 'content-1', ?, ?, 'text/plain')
            """,
            (f"acquisition:{fragment_id}", asset_id, item_id, definition_hash, _NOW.isoformat()),
        )
        _ = connection.execute(
            """
            INSERT INTO evidence_fragments (
                fragment_id, asset_id, fragment_kind, locator_json, extraction_method
            ) VALUES (?, ?, 'web_span', '{}', 'manual')
            """,
            (fragment_id, asset_id),
        )


def _observation() -> ClaimObservation:
    return ClaimObservation(
        observation_id="observation-1",
        claim_text="Revenue increased.",
        claim_kind=ClaimKind.FACTUAL,
        category=ClaimCategory.FUNDAMENTAL,
        source_item_id="origin-item",
        evidence_fragment_ids=("origin-fragment",),
        asserted_at=_NOW,
        known_at=_NOW,
        effective_from=_NOW,
        horizon_class=HorizonClass.TACTICAL,
    )


@pytest.fixture
def verified_claim(tmp_path: Path) -> tuple[ClaimRepository, Database]:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    _register_evidence(
        database,
        source_id="origin",
        provenance_group="origin-group",
        allowed_use=AllowedUse.INTERPRETATION,
        trust_level=TrustLevel.COMMENTARY,
        item_id="origin-item",
        fragment_id="origin-fragment",
        asset_id="a" * 64,
    )
    _register_evidence(
        database,
        source_id="primary",
        provenance_group="primary-group",
        allowed_use=AllowedUse.FACTUAL_VERIFICATION,
        trust_level=TrustLevel.AUTHORITATIVE_PRIMARY,
        item_id="primary-item",
        fragment_id="primary-fragment",
        asset_id="b" * 64,
    )
    repository = _claim_repository(database)
    repository.append_observation(_observation())
    return repository, database


def _verification(
    *,
    status: VerificationStatus = VerificationStatus.SUPPORTED,
    fragment_id: str = "primary-fragment",
    groups: tuple[str, ...] = ("primary-group",),
) -> VerificationResult:
    return VerificationResult(
        verification_id="verification-1",
        observation_id="observation-1",
        status=status,
        supporting_evidence_ids=(fragment_id,),
        independent_provenance_groups=groups,
        checked_at=_NOW,
        known_at=_NOW,
        verifier_version="verifier-1",
    )


def test_append_verification_rederives_supported_status_and_groups(
    verified_claim: tuple[ClaimRepository, Database],
) -> None:
    repository, _database = verified_claim
    verification = _verification()

    repository.append_verification(verification)

    assert repository.verifications_by_ids((verification.verification_id,)) == (verification,)


def test_verification_evidence_authority_uses_durable_policy_and_excludes_origin(
    verified_claim: tuple[ClaimRepository, Database],
) -> None:
    repository, _database = verified_claim
    repository.append_resolution(
        ClaimResolutionDecision(
            decision_id="resolution-1",
            subject_observation_id="observation-1",
            relation=ClaimResolutionKind.DISTINCT,
            decided_at=_NOW,
            known_at=_NOW,
            resolver_version="resolver-1",
            rationale="No prior canonical claim matched this observation.",
        )
    )

    authority = repository.verification_evidence_authority(
        deterministic_claim_key("Revenue increased."),
        ("origin-fragment", "primary-fragment"),
        as_of=_NOW,
    )

    assert tuple(item.fragment_id for item in authority) == ("primary-fragment",)
    assert authority[0].provenance_group == "primary-group"
    assert authority[0].trust_level is TrustLevel.AUTHORITATIVE_PRIMARY
    assert authority[0].allowed_uses == (AllowedUse.FACTUAL_VERIFICATION,)


def test_append_verification_records_support_only_groups_for_mixed_result(
    verified_claim: tuple[ClaimRepository, Database],
) -> None:
    repository, database = verified_claim
    _register_evidence(
        database,
        source_id="contradiction",
        provenance_group="contradiction-group",
        allowed_use=AllowedUse.FACTUAL_VERIFICATION,
        trust_level=TrustLevel.AUTHORITATIVE_PRIMARY,
        item_id="contradiction-item",
        fragment_id="contradiction-fragment",
        asset_id="c" * 64,
    )
    verification = VerificationResult(
        verification_id="verification-mixed",
        observation_id="observation-1",
        status=VerificationStatus.MIXED,
        supporting_evidence_ids=("primary-fragment",),
        contradicting_evidence_ids=("contradiction-fragment",),
        independent_provenance_groups=("primary-group",),
        checked_at=_NOW,
        known_at=_NOW,
        verifier_version="verifier-1",
    )

    repository.append_verification(verification)

    assert repository.verifications_by_ids((verification.verification_id,)) == (verification,)


@pytest.mark.parametrize(
    "verification",
    [
        _verification(status=VerificationStatus.CONTRADICTED),
        _verification(groups=("invented-group",)),
        _verification(fragment_id="origin-fragment", groups=("origin-group",)),
    ],
)
def test_append_verification_rejects_caller_invented_or_originating_authority(
    verified_claim: tuple[ClaimRepository, Database],
    verification: VerificationResult,
) -> None:
    repository, _database = verified_claim

    with pytest.raises(ClaimVerificationPolicyError):
        repository.append_verification(verification)
