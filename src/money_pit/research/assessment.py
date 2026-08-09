"""Deterministic material-anchor assessment from durable verifications."""

from __future__ import annotations

from typing import TYPE_CHECKING

from money_pit.claims.repository import ClaimNotFoundError
from money_pit.claims.repository import deterministic_claim_key
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import VerificationStatus
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import ProvisionalAnchorEvidence
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import TrustLevel


if TYPE_CHECKING:
    from datetime import datetime

    from money_pit.claims.repository import ClaimRepository
    from money_pit.pipeline.interpretation import InterpretationOutcome
    from money_pit.research.service import ResearchInterpretationTarget
    from money_pit.schemas.claims import VerificationEvidenceAuthority
    from money_pit.schemas.research import EvidenceAliasBinding


class ClaimVerificationMaterialAssessor:
    """Assess anchors only from persisted, point-in-time verification results."""

    def __init__(self, claims: ClaimRepository) -> None:
        """Bind the durable point-in-time claim repository."""
        self._claims: ClaimRepository = claims

    def assess(
        self,
        material_claim_keys: tuple[str, ...],
        *,
        bindings: tuple[EvidenceAliasBinding, ...],
        decision_at: datetime,
        provisional_targets: tuple[ResearchInterpretationTarget, ...] = (),
        outcomes: tuple[InterpretationOutcome, ...] = (),
    ) -> MaterialAnchorAssessment:
        """Classify supported, contradicted, and unresolved material anchors."""
        supported: list[str] = []
        contradicted: list[str] = []
        unresolved: list[str] = []
        provenance: set[str] = set()
        has_primary = False
        for key in material_claim_keys:
            try:
                _, verifications = self._claims.history_as_of(key, as_of=decision_at)
            except ClaimNotFoundError:
                unresolved.append(key)
                continue
            latest = max(verifications, key=lambda item: (item.known_at, item.checked_at), default=None)
            if latest is None:
                unresolved.append(key)
                continue
            supporting_authority = self._claims.verification_evidence_authority(
                key,
                tuple(dict.fromkeys(latest.supporting_evidence_ids)),
                as_of=decision_at,
            )
            contradicting_authority = self._claims.verification_evidence_authority(
                key,
                tuple(dict.fromkeys(latest.contradicting_evidence_ids)),
                as_of=decision_at,
            )
            supporting_groups = {item.provenance_group for item in supporting_authority}
            contradicting_groups = {item.provenance_group for item in contradicting_authority}
            authoritative = any(
                item.trust_level is TrustLevel.AUTHORITATIVE_PRIMARY
                and AllowedUse.FACTUAL_VERIFICATION in item.allowed_uses
                for item in supporting_authority
            )
            has_primary = has_primary or authoritative
            if latest.status is VerificationStatus.CONTRADICTED and _authority_meets_standard(contradicting_authority):
                contradicted.append(key)
                provenance.update(contradicting_groups)
            elif latest.status is VerificationStatus.SUPPORTED and _authority_meets_standard(supporting_authority):
                supported.append(key)
                provenance.update(supporting_groups)
            else:
                unresolved.append(key)
        provisional, provisional_provenance, provisional_primary, provisional_evidence = _provisional_coverage(
            material_claim_keys,
            bindings=bindings,
            targets=provisional_targets,
            outcomes=outcomes,
        )
        contradicted_set = set(contradicted)
        provisional = tuple(key for key in provisional if key not in contradicted_set and key not in supported)
        unresolved = [
            key
            for key in material_claim_keys
            if key not in supported and key not in contradicted_set and key not in provisional
        ]
        provenance.update(provisional_provenance)
        has_primary = has_primary or provisional_primary
        return MaterialAnchorAssessment(
            material_claim_keys=material_claim_keys,
            supported_claim_keys=tuple(supported),
            provisionally_covered_claim_keys=provisional,
            provisional_evidence=provisional_evidence,
            contradicted_claim_keys=tuple(contradicted),
            unresolved_claim_keys=tuple(unresolved),
            independent_provenance_groups=tuple(sorted(provenance)),
            has_authoritative_primary=has_primary,
            evidence_standard_satisfied=bool(material_claim_keys) and not unresolved and not contradicted,
            decisive_contradiction=bool(contradicted),
        )


def _authority_meets_standard(values: tuple[VerificationEvidenceAuthority, ...]) -> bool:
    eligible = tuple(
        item
        for item in values
        if AllowedUse.FACTUAL_VERIFICATION in item.allowed_uses
        and item.trust_level in {TrustLevel.AUTHORITATIVE_PRIMARY, TrustLevel.INDEPENDENT_SECONDARY}
    )
    return (
        any(item.trust_level is TrustLevel.AUTHORITATIVE_PRIMARY for item in eligible)
        or len({item.provenance_group for item in eligible}) >= 2
    )


def _provisional_coverage(
    material_claim_keys: tuple[str, ...],
    *,
    bindings: tuple[EvidenceAliasBinding, ...],
    targets: tuple[ResearchInterpretationTarget, ...],
    outcomes: tuple[InterpretationOutcome, ...],
) -> tuple[tuple[str, ...], set[str], bool, tuple[ProvisionalAnchorEvidence, ...]]:
    """Assess targeted factual coverage without persisting a verification decision."""
    outcome_by_document = {outcome.document_id: outcome for outcome in outcomes}
    groups_by_key: dict[str, set[str]] = {key: set() for key in material_claim_keys}
    primary_by_key: dict[str, bool] = dict.fromkeys(material_claim_keys, False)
    all_groups: set[str] = set()
    for target in targets:
        outcome = outcome_by_document.get(target.work.document.asset.asset_id)
        if outcome is None:
            continue
        for observation in outcome.observations:
            if observation.claim_kind is not ClaimKind.FACTUAL:
                continue
            cited_ids = set(observation.evidence_fragment_ids)
            eligible_bindings = tuple(
                binding
                for binding in bindings
                if binding.source_item_id == observation.source_item_id
                and AllowedUse.FACTUAL_VERIFICATION in binding.allowed_uses
                and cited_ids.intersection(binding.fragment_ids)
            )
            for key in target.material_claim_keys:
                if key not in groups_by_key:
                    continue
                if deterministic_claim_key(observation.claim_text) != key:
                    continue
                groups = {binding.provenance_group for binding in eligible_bindings}
                groups_by_key[key].update(groups)
                all_groups.update(groups)
                if any(binding.trust_level is TrustLevel.AUTHORITATIVE_PRIMARY for binding in eligible_bindings):
                    primary_by_key[key] = True
    covered = tuple(key for key in material_claim_keys if primary_by_key[key] or len(groups_by_key[key]) >= 2)
    evidence = tuple(
        ProvisionalAnchorEvidence(
            claim_key=key,
            provenance_groups=tuple(sorted(groups_by_key[key])),
            has_authoritative_primary=primary_by_key[key],
        )
        for key in material_claim_keys
        if groups_by_key[key]
    )
    return covered, all_groups, any(primary_by_key.values()), evidence
