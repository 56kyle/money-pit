"""Pure decision-material identity shared by runtime and storage migration."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


if TYPE_CHECKING:
    from collections.abc import Mapping

    from money_pit.schemas.claims import CanonicalClaim
    from money_pit.schemas.research import EvidenceAliasBinding
    from money_pit.schemas.research import ResearchCumulativeContext
    from money_pit.schemas.research import ResearchEvidenceRecord


SYNTHESIS_MATERIAL_POLICY_VERSION = "synthesis-material@1"
_FROZEN_CONFIG: ConfigDict = ConfigDict(frozen=True, extra="forbid")


class SynthesisDecision(StrEnum):
    """Deterministic disposition before any A4 invocation."""

    ELIGIBLE = "eligible"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class AcceptedEvidenceIdentity(BaseModel):
    """Stable provenance identity of evidence admitted to synthesis."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    source_item_id: str = Field(min_length=1)
    fragment_ids: tuple[str, ...] = Field(min_length=1)
    provenance_group: str = Field(min_length=1)
    trust_level: str = Field(min_length=1)
    allowed_uses: tuple[str, ...] = Field(min_length=1)


class SynthesisMaterialProjection(BaseModel):
    """Economic state whose changes alone may justify another A4 call."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    hypothesis_id: str = Field(min_length=1)
    grounded_observation_ids: tuple[str, ...]
    material_claims: tuple[dict[str, object], ...]
    accepted_evidence: tuple[AcceptedEvidenceIdentity, ...]
    supported_claim_keys: tuple[str, ...]
    provisionally_covered_claim_keys: tuple[str, ...]
    contradicted_claim_keys: tuple[str, ...]
    unresolved_claim_keys: tuple[str, ...]
    evidence_standard_satisfied: bool
    decisive_contradiction: bool
    prior_revision_id: str | None
    policy_version: str = SYNTHESIS_MATERIAL_POLICY_VERSION

    @property
    def material_state_id(self) -> str:
        """Return one identity shared by operationally different research histories."""
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"synthesis-material:{hashlib.sha256(encoded).hexdigest()}"

    @property
    def decision(self) -> SynthesisDecision:
        """Return whether inference can add decision-relevant information."""
        assessment_passed = self.evidence_standard_satisfied or self.decisive_contradiction
        return SynthesisDecision.ELIGIBLE if assessment_passed else SynthesisDecision.INSUFFICIENT_EVIDENCE


def project_synthesis_material(
    *,
    hypothesis_id: str,
    grounded_observation_ids: tuple[str, ...],
    material_claims: tuple[CanonicalClaim, ...],
    research_context: ResearchCumulativeContext,
    prior_revision_id: str | None,
) -> SynthesisMaterialProjection:
    """Project synthesis identity without runs, sessions, queries, failures, or timestamps."""
    assessment = research_context.material_anchor_assessment
    evidence = tuple(
        sorted(
            {
                (
                    binding.source_item_id,
                    tuple(sorted(binding.fragment_ids)),
                    binding.provenance_group,
                    binding.trust_level.value,
                    tuple(sorted(item.value for item in binding.allowed_uses)),
                ): AcceptedEvidenceIdentity(
                    source_item_id=binding.source_item_id,
                    fragment_ids=tuple(sorted(binding.fragment_ids)),
                    provenance_group=binding.provenance_group,
                    trust_level=binding.trust_level.value,
                    allowed_uses=tuple(sorted(item.value for item in binding.allowed_uses)),
                )
                for binding in research_context.alias_bindings
            }.values(),
            key=lambda item: item.model_dump_json(),
        )
    )
    return project_synthesis_material_from_components(
        hypothesis_id=hypothesis_id,
        grounded_observation_ids=grounded_observation_ids,
        material_claims=tuple(claim.model_dump(mode="json") for claim in material_claims),
        accepted_evidence=evidence,
        supported_claim_keys=assessment.supported_claim_keys,
        provisionally_covered_claim_keys=assessment.provisionally_covered_claim_keys,
        contradicted_claim_keys=assessment.contradicted_claim_keys,
        unresolved_claim_keys=assessment.unresolved_claim_keys,
        evidence_standard_satisfied=assessment.evidence_standard_satisfied,
        decisive_contradiction=assessment.decisive_contradiction,
        prior_revision_id=prior_revision_id,
    )


def project_synthesis_material_from_components(
    *,
    hypothesis_id: str,
    grounded_observation_ids: tuple[str, ...],
    material_claims: tuple[Mapping[str, object], ...],
    accepted_evidence: tuple[AcceptedEvidenceIdentity, ...],
    supported_claim_keys: tuple[str, ...],
    provisionally_covered_claim_keys: tuple[str, ...],
    contradicted_claim_keys: tuple[str, ...],
    unresolved_claim_keys: tuple[str, ...],
    evidence_standard_satisfied: bool,
    decisive_contradiction: bool,
    prior_revision_id: str | None,
) -> SynthesisMaterialProjection:
    """Build the sole material identity from runtime or migrated decision-state components."""
    claims = tuple(
        sorted(
            (
                {key: value for key, value in claim.items() if key not in {"projected_as_of", "next_refresh_at"}}
                for claim in material_claims
            ),
            key=lambda claim: json.dumps(claim, sort_keys=True, separators=(",", ":"), default=str),
        )
    )
    evidence = tuple(
        sorted(
            {item.model_dump_json(): item for item in accepted_evidence}.values(),
            key=lambda item: item.model_dump_json(),
        )
    )
    return SynthesisMaterialProjection(
        hypothesis_id=hypothesis_id,
        grounded_observation_ids=tuple(sorted(set(grounded_observation_ids))),
        material_claims=claims,
        accepted_evidence=evidence,
        supported_claim_keys=tuple(sorted(set(supported_claim_keys))),
        provisionally_covered_claim_keys=tuple(sorted(set(provisionally_covered_claim_keys))),
        contradicted_claim_keys=tuple(sorted(set(contradicted_claim_keys))),
        unresolved_claim_keys=tuple(sorted(set(unresolved_claim_keys))),
        evidence_standard_satisfied=evidence_standard_satisfied,
        decisive_contradiction=decisive_contradiction,
        prior_revision_id=prior_revision_id,
    )


def merge_research_contexts(
    contexts: tuple[ResearchCumulativeContext, ...],
) -> ResearchCumulativeContext:
    """Merge round contexts while discarding operational ordering from their assessment."""
    from money_pit.schemas.research import MaterialAnchorAssessment
    from money_pit.schemas.research import ProvisionalAnchorEvidence
    from money_pit.schemas.research import ResearchCumulativeContext

    evidence: list[ResearchEvidenceRecord] = []
    bindings: list[EvidenceAliasBinding] = []
    counters = {"E": 0, "F": 0, "T": 0}
    for context in contexts:
        evidence_by_alias = {item.alias: item for item in context.evidence}
        bindings_by_alias = {item.alias: item for item in context.alias_bindings}
        if len(evidence_by_alias) != len(context.evidence) or len(bindings_by_alias) != len(context.alias_bindings):
            raise ValueError("Research context aliases must be unique")
        if evidence_by_alias.keys() != bindings_by_alias.keys():
            raise ValueError("Research evidence and bindings must have identical aliases")
        for item in context.evidence:
            prefix = item.alias[0]
            counters[prefix] += 1
            alias = f"{prefix}{counters[prefix]:06d}"
            evidence.append(item.model_copy(update={"alias": alias}))
            bindings.append(bindings_by_alias[item.alias].model_copy(update={"alias": alias}))
    material_keys = tuple(
        dict.fromkeys(key for context in contexts for key in context.material_anchor_assessment.material_claim_keys)
    )
    supported = tuple(
        dict.fromkeys(key for context in contexts for key in context.material_anchor_assessment.supported_claim_keys)
    )
    contradicted = tuple(
        dict.fromkeys(key for context in contexts for key in context.material_anchor_assessment.contradicted_claim_keys)
    )
    provisional_groups: dict[str, set[str]] = {}
    provisional_primary: dict[str, bool] = {}
    for context in contexts:
        for item in context.material_anchor_assessment.provisional_evidence:
            provisional_groups.setdefault(item.claim_key, set()).update(item.provenance_groups)
            provisional_primary[item.claim_key] = (
                provisional_primary.get(item.claim_key, False) or item.has_authoritative_primary
            )
    provisional_evidence = tuple(
        ProvisionalAnchorEvidence(
            claim_key=key,
            provenance_groups=tuple(sorted(groups)),
            has_authoritative_primary=provisional_primary.get(key, False),
        )
        for key, groups in sorted(provisional_groups.items())
    )
    provisional = tuple(
        item.claim_key
        for item in provisional_evidence
        if (item.has_authoritative_primary or len(item.provenance_groups) >= 2)
        and item.claim_key not in supported
        and item.claim_key not in contradicted
    )
    unresolved = tuple(
        key for key in material_keys if key not in supported and key not in provisional and key not in contradicted
    )
    provenance = tuple(sorted({group for context in contexts for group in context.provenance_groups}))
    return ResearchCumulativeContext(
        new_observations=tuple(
            {item.observation_id: item for context in contexts for item in context.new_observations}.values()
        ),
        evidence=tuple(evidence),
        alias_bindings=tuple(bindings),
        provenance_groups=provenance,
        failure_kinds=tuple(kind for context in contexts for kind in context.failure_kinds),
        material_anchor_assessment=MaterialAnchorAssessment(
            material_claim_keys=material_keys,
            supported_claim_keys=supported,
            provisionally_covered_claim_keys=provisional,
            provisional_evidence=provisional_evidence,
            contradicted_claim_keys=contradicted,
            unresolved_claim_keys=unresolved,
            independent_provenance_groups=provenance,
            has_authoritative_primary=any(
                context.material_anchor_assessment.has_authoritative_primary for context in contexts
            ),
            evidence_standard_satisfied=bool(material_keys) and not unresolved and not contradicted,
            decisive_contradiction=any(
                context.material_anchor_assessment.decisive_contradiction for context in contexts
            ),
        ),
    )


def canonical_synthesis_context(context: ResearchCumulativeContext) -> ResearchCumulativeContext:
    """Normalize model-visible evidence while excluding operational failure history."""
    from money_pit.schemas.research import ResearchCumulativeContext

    evidence_by_alias = {item.alias: item for item in context.evidence}
    ordered_bindings = sorted(
        context.alias_bindings,
        key=lambda item: (
            item.source_item_id,
            tuple(sorted(item.fragment_ids)),
            item.provenance_group,
            item.trust_level.value,
            tuple(sorted(value.value for value in item.allowed_uses)),
        ),
    )
    evidence: list[ResearchEvidenceRecord] = []
    bindings: list[EvidenceAliasBinding] = []
    counters = {"E": 0, "F": 0, "T": 0}
    for binding in ordered_bindings:
        item = evidence_by_alias.get(binding.alias)
        if item is None:
            raise ValueError("Research binding has no model-visible evidence")
        prefix = item.alias[0]
        counters[prefix] += 1
        alias = f"{prefix}{counters[prefix]:06d}"
        evidence.append(item.model_copy(update={"alias": alias}))
        bindings.append(binding.model_copy(update={"alias": alias}))
    return ResearchCumulativeContext(
        new_observations=tuple(sorted(context.new_observations, key=lambda item: item.observation_id)),
        evidence=tuple(evidence),
        alias_bindings=tuple(bindings),
        provenance_groups=tuple(sorted(set(context.provenance_groups))),
        failure_kinds=(),
        material_anchor_assessment=context.material_anchor_assessment.model_copy(
            update={
                "material_claim_keys": tuple(sorted(set(context.material_anchor_assessment.material_claim_keys))),
                "supported_claim_keys": tuple(sorted(set(context.material_anchor_assessment.supported_claim_keys))),
                "provisionally_covered_claim_keys": tuple(
                    sorted(set(context.material_anchor_assessment.provisionally_covered_claim_keys))
                ),
                "contradicted_claim_keys": tuple(
                    sorted(set(context.material_anchor_assessment.contradicted_claim_keys))
                ),
                "unresolved_claim_keys": tuple(sorted(set(context.material_anchor_assessment.unresolved_claim_keys))),
                "independent_provenance_groups": tuple(
                    sorted(set(context.material_anchor_assessment.independent_provenance_groups))
                ),
            }
        ),
    )
