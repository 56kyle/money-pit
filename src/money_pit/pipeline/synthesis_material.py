"""Compatibility exports for the storage-independent synthesis material authority."""

from money_pit.synthesis_material import SYNTHESIS_MATERIAL_POLICY_VERSION
from money_pit.synthesis_material import AcceptedEvidenceIdentity
from money_pit.synthesis_material import SynthesisDecision
from money_pit.synthesis_material import SynthesisMaterialProjection
from money_pit.synthesis_material import canonical_synthesis_context
from money_pit.synthesis_material import merge_research_contexts
from money_pit.synthesis_material import project_synthesis_material
from money_pit.synthesis_material import project_synthesis_material_from_components


__all__ = (
    "SYNTHESIS_MATERIAL_POLICY_VERSION",
    "AcceptedEvidenceIdentity",
    "SynthesisDecision",
    "SynthesisMaterialProjection",
    "canonical_synthesis_context",
    "merge_research_contexts",
    "project_synthesis_material",
    "project_synthesis_material_from_components",
)
