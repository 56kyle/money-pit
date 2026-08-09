from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from money_pit.claims.repository import ClaimNotFoundError
from money_pit.claims.repository import ClaimRepository
from money_pit.claims.repository import deterministic_claim_key
from money_pit.evidence.work import EvidenceInterpretationWork
from money_pit.pipeline.interpretation import InterpretationOutcome
from money_pit.research.assessment import ClaimVerificationMaterialAssessor
from money_pit.research.service import ResearchInterpretationTarget
from money_pit.research.service import ResearchSourceAssociation
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.claims import VerificationEvidenceAuthority
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.research import EvidenceAliasBinding
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.sources._shared import source_definition_hash


NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _source_association(source_item_id: str) -> ResearchSourceAssociation:
    definition = SourceDefinition(
        source_id="issuer",
        adapter_name="research",
        locator="https://issuer.test",
        provenance_group="issuer-primary",
        allowed_uses=(AllowedUse.FACTUAL_VERIFICATION,),
        trust_settings=(
            SourceTrustSetting(
                category=TrustCategory.FACTUAL,
                level=TrustLevel.AUTHORITATIVE_PRIMARY,
            ),
        ),
    )
    return ResearchSourceAssociation(
        source_item_id=source_item_id,
        source_definition_hash=source_definition_hash(definition),
        source_definition=definition,
    )


class _ClaimHistory:
    def __init__(
        self,
        histories: dict[str, tuple[VerificationResult, ...]],
        authorities: dict[tuple[str, str], tuple[VerificationEvidenceAuthority, ...]] | None = None,
    ) -> None:
        self._histories: dict[str, tuple[VerificationResult, ...]] = histories
        self._authorities: dict[tuple[str, str], tuple[VerificationEvidenceAuthority, ...]] = authorities or {}

    def history_as_of(
        self,
        canonical_claim_key: str,
        *,
        as_of: datetime,
    ) -> tuple[tuple[object, ...], tuple[VerificationResult, ...]]:
        del as_of
        try:
            return (), self._histories[canonical_claim_key]
        except KeyError as error:
            raise ClaimNotFoundError(canonical_claim_key) from error

    def verification_evidence_authority(
        self,
        canonical_claim_key: str,
        fragment_ids: tuple[str, ...],
        *,
        as_of: datetime,
    ) -> tuple[VerificationEvidenceAuthority, ...]:
        del as_of
        return tuple(
            item
            for fragment_id in fragment_ids
            for item in self._authorities.get((canonical_claim_key, fragment_id), ())
        )


def _verification(
    status: VerificationStatus,
    *,
    supporting: tuple[str, ...] = (),
    contradicting: tuple[str, ...] = (),
    groups: tuple[str, ...] = (),
    known_at: datetime = NOW,
) -> VerificationResult:
    return VerificationResult(
        verification_id=f"verification:{status}:{known_at.isoformat()}",
        observation_id="observation-1",
        status=status,
        supporting_evidence_ids=supporting,
        contradicting_evidence_ids=contradicting,
        independent_provenance_groups=groups,
        checked_at=known_at,
        known_at=known_at,
        verifier_version="test",
    )


def _authority(
    fragment_id: str,
    provenance_group: str,
    trust_level: TrustLevel,
) -> VerificationEvidenceAuthority:
    return VerificationEvidenceAuthority(
        fragment_id=fragment_id,
        asset_id=f"asset-{fragment_id}",
        source_item_id=f"source:{fragment_id}",
        source_definition_hash="a" * 64,
        provenance_group=provenance_group,
        trust_category=TrustCategory.FACTUAL,
        trust_level=trust_level,
        allowed_uses=(AllowedUse.FACTUAL_VERIFICATION,),
    )


@pytest.mark.parametrize(
    ("verification", "authorities"),
    [
        (
            _verification(VerificationStatus.SUPPORTED, supporting=("primary",), groups=("one",)),
            {("claim", "primary"): (_authority("primary", "one", TrustLevel.AUTHORITATIVE_PRIMARY),)},
        ),
        (
            _verification(
                VerificationStatus.SUPPORTED,
                supporting=("secondary-one", "secondary-two"),
                groups=("one", "two"),
            ),
            {
                ("claim", "secondary-one"): (_authority("secondary-one", "one", TrustLevel.INDEPENDENT_SECONDARY),),
                ("claim", "secondary-two"): (_authority("secondary-two", "two", TrustLevel.INDEPENDENT_SECONDARY),),
            },
        ),
    ],
)
def test_assess_requires_primary_support_or_two_independent_groups(
    verification: VerificationResult,
    authorities: dict[tuple[str, str], tuple[VerificationEvidenceAuthority, ...]],
) -> None:
    claims = _ClaimHistory({"claim": (verification,)}, authorities)
    assessor = ClaimVerificationMaterialAssessor(cast("ClaimRepository", cast("object", claims)))

    result = assessor.assess(("claim",), bindings=(), decision_at=NOW)

    assert result.supported_claim_keys == ("claim",)
    assert result.evidence_standard_satisfied is True


def test_assess_does_not_cover_an_anchor_with_an_unrelated_authoritative_fact() -> None:
    asset_id = "b" * 64
    fragment_id = "fragment-unrelated"
    source_item_id = "issuer:item"
    document = EvidenceDocument(
        asset=EvidenceAsset(
            asset_id=asset_id,
            content_hash=asset_id,
            media_type="text/plain",
            source_item_id=source_item_id,
            local_path=Path("bb") / asset_id,
            retrieved_at=NOW,
        ),
        fragments=(
            EvidenceFragment(
                fragment_id=fragment_id,
                asset_id=asset_id,
                kind="web_span",
                locator=TextLocator(start_offset=0, end_offset=4),
                extracted_text="fact",
                extraction_method="test",
            ),
        ),
    )
    target_key = deterministic_claim_key("Revenue increased")
    target = ResearchInterpretationTarget(
        work=EvidenceInterpretationWork(document=document, content_version="version-1"),
        source=_source_association(source_item_id),
        material_claim_keys=(target_key,),
    )
    outcome = InterpretationOutcome(
        attempt_id="attempt-unrelated",
        document_id=asset_id,
        fragment_ids=(fragment_id,),
        observations=(
            ClaimObservation(
                observation_id="observation-unrelated",
                claim_text="The issuer appointed a new director",
                claim_kind=ClaimKind.FACTUAL,
                category=ClaimCategory.CATALYST,
                source_item_id=source_item_id,
                evidence_fragment_ids=(fragment_id,),
                asserted_at=NOW,
                known_at=NOW,
                horizon_class=HorizonClass.TACTICAL,
            ),
        ),
        requests=(),
        responses=(),
        alias_maps=(),
        known_at=NOW,
        completed_at=NOW,
    )
    binding = EvidenceAliasBinding(
        alias="E000001",
        fragment_ids=(fragment_id,),
        source_item_id=source_item_id,
        provenance_group="issuer-primary",
        allowed_uses=(AllowedUse.INTERPRETATION, AllowedUse.FACTUAL_VERIFICATION),
        trust_level=TrustLevel.AUTHORITATIVE_PRIMARY,
    )
    assessor = ClaimVerificationMaterialAssessor(cast("ClaimRepository", cast("object", _ClaimHistory({}))))

    result = assessor.assess(
        (target_key,),
        bindings=(binding,),
        decision_at=NOW,
        provisional_targets=(target,),
        outcomes=(outcome,),
    )

    assert result.provisionally_covered_claim_keys == ()
    assert result.unresolved_claim_keys == (target_key,)
    assert result.evidence_standard_satisfied is False


def test_assess_uses_the_latest_contradiction_as_a_decisive_stop() -> None:
    claims = _ClaimHistory(
        {
            "claim": (
                _verification(VerificationStatus.SUPPORTED, groups=("one", "two")),
                _verification(
                    VerificationStatus.CONTRADICTED,
                    contradicting=("contradiction",),
                    known_at=datetime(2026, 8, 9, 1, tzinfo=UTC),
                ),
            ),
        },
        {("claim", "contradiction"): (_authority("contradiction", "issuer", TrustLevel.AUTHORITATIVE_PRIMARY),)},
    )
    assessor = ClaimVerificationMaterialAssessor(cast("ClaimRepository", cast("object", claims)))

    result = assessor.assess(("claim",), bindings=(), decision_at=NOW)

    assert result.contradicted_claim_keys == ("claim",)
    assert result.decisive_contradiction is True


@pytest.mark.parametrize(
    "histories",
    [{}, {"claim": (_verification(VerificationStatus.MIXED, groups=("one", "two")),)}],
)
def test_assess_keeps_missing_or_mixed_claims_unresolved(
    histories: dict[str, tuple[VerificationResult, ...]],
) -> None:
    claims = _ClaimHistory(histories)
    assessor = ClaimVerificationMaterialAssessor(cast("ClaimRepository", cast("object", claims)))

    result = assessor.assess(("claim",), bindings=(), decision_at=NOW)

    assert result.unresolved_claim_keys == ("claim",)
    assert result.evidence_standard_satisfied is False


def test_assess_marks_new_authoritative_factual_evidence_as_provisional_coverage() -> None:
    asset_id = "a" * 64
    fragment_id = "fragment-authoritative"
    source_item_id = "issuer:item"
    document = EvidenceDocument(
        asset=EvidenceAsset(
            asset_id=asset_id,
            content_hash=asset_id,
            media_type="text/plain",
            source_item_id=source_item_id,
            local_path=Path("aa") / asset_id,
            retrieved_at=NOW,
        ),
        fragments=(
            EvidenceFragment(
                fragment_id=fragment_id,
                asset_id=asset_id,
                kind="web_span",
                locator=TextLocator(start_offset=0, end_offset=4),
                extracted_text="fact",
                extraction_method="test",
            ),
        ),
    )
    claim_key = deterministic_claim_key("Revenue increased")
    target = ResearchInterpretationTarget(
        work=EvidenceInterpretationWork(document=document, content_version="version-1"),
        source=_source_association(source_item_id),
        material_claim_keys=(claim_key,),
    )
    observation = ClaimObservation(
        observation_id="observation-new",
        claim_text="Revenue increased",
        claim_kind=ClaimKind.FACTUAL,
        category=ClaimCategory.FUNDAMENTAL,
        source_item_id=source_item_id,
        evidence_fragment_ids=(fragment_id,),
        asserted_at=NOW,
        known_at=NOW,
        horizon_class=HorizonClass.TACTICAL,
    )
    outcome = InterpretationOutcome(
        attempt_id="attempt-new",
        document_id=asset_id,
        fragment_ids=(fragment_id,),
        observations=(observation,),
        requests=(),
        responses=(),
        alias_maps=(),
        known_at=NOW,
        completed_at=NOW,
    )
    binding = EvidenceAliasBinding(
        alias="E000001",
        fragment_ids=(fragment_id,),
        source_item_id=source_item_id,
        provenance_group="issuer-primary",
        allowed_uses=(AllowedUse.INTERPRETATION, AllowedUse.FACTUAL_VERIFICATION),
        trust_level=TrustLevel.AUTHORITATIVE_PRIMARY,
    )
    assessor = ClaimVerificationMaterialAssessor(
        cast("ClaimRepository", cast("object", _ClaimHistory({}))),
    )

    result = assessor.assess(
        (claim_key,),
        bindings=(binding,),
        decision_at=NOW,
        provisional_targets=(target,),
        outcomes=(outcome,),
    )

    assert result.supported_claim_keys == ()
    assert result.provisionally_covered_claim_keys == (claim_key,)
    assert result.unresolved_claim_keys == ()
    assert result.evidence_standard_satisfied is True
