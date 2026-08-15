"""Module deriving stable economic identities from immutable intelligence records."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Protocol
from typing import cast

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.schemas.claims import HorizonClass  # noqa: TC001 - Pydantic resolves the enum at runtime
from money_pit.schemas.theses import ThesisDirection  # noqa: TC001 - Pydantic resolves the enum at runtime


if TYPE_CHECKING:
    from collections.abc import Mapping

    from money_pit.schemas.claims import CanonicalClaim
    from money_pit.schemas.theses import CandidateThesis


_FROZEN_CONFIG: ConfigDict = ConfigDict(frozen=True, extra="forbid")
HYPOTHESIS_REVIEW_POLICY_VERSION = "hypothesis-review@1"
CANONICAL_HYPOTHESIS_GROUP_POLICY_VERSION = "canonical-hypothesis-group@1"
RESEARCH_SCOPE_POLICY_VERSION = "candidate-research-scope@1"


class ResearchTaskSemanticInput(Protocol):
    """Minimum executable task contract required by semantic identity."""

    provider: str
    query: str
    purpose: str
    maximum_results: int
    material_claim_keys: tuple[str, ...]


class CapitalReferenceKind(StrEnum):
    """Authority used to identify the capital subject of a hypothesis."""

    RESOLVED_INSTRUMENT = "resolved_instrument"
    INSTRUMENT_REFERENCE = "instrument_reference"
    UNIVERSE_REFERENCE = "universe_reference"
    UNCLASSIFIED_CANDIDATE = "unclassified_candidate"


class CandidateSemanticVariant(BaseModel):
    """Exact structured economic meaning of one immutable candidate proposal."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    capital_kind: CapitalReferenceKind
    capital_reference: str = Field(min_length=1)
    direction: ThesisDirection
    horizon_class: HorizonClass
    theme: str | None = None
    causal_mechanisms: tuple[str, ...] = ()
    regime_assumptions: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        """Return the stable exact-match fingerprint for this variant."""
        return _fingerprint(self.model_dump(mode="json"))

    @property
    def variant_id(self) -> str:
        """Return this exact variant identity without inventing group authority."""
        return f"hypothesis-variant:{self.fingerprint}"

    @property
    def capital_available(self) -> bool:
        """Return whether the proposal has explicit capital-reference authority."""
        return self.capital_kind is not CapitalReferenceKind.UNCLASSIFIED_CANDIDATE


class ResearchTaskSemantics(BaseModel):
    """Candidate-independent semantics of one immutable initial research task."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    provider: str = Field(min_length=1)
    query: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    material_claim_keys: tuple[str, ...] = ()
    maximum_results: int = Field(ge=1, le=20)


class ResearchPremiseSemantics(BaseModel):
    """Material research premise excluding queue and planner progress state."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    hypothesis_id: str = Field(min_length=1)
    material_claims: tuple[dict[str, object], ...] = ()
    initial_tasks: tuple[ResearchTaskSemantics, ...] = ()

    @property
    def fingerprint(self) -> str:
        """Return the premise fingerprint used to version one research case."""
        return _fingerprint(self.model_dump(mode="json"))


def research_case_id(hypothesis_id: str, scope_fingerprint: str) -> str:
    """Return the canonical case identity shared by migration and runtime reconciliation."""
    return f"research-case:{_fingerprint({'hypothesis_id': hypothesis_id, 'scope_fingerprint': scope_fingerprint})}"


def canonical_hypothesis_group_id(variant_ids: tuple[str, ...]) -> str:
    """Return an order-independent group identity while provenance stays in merge edges."""
    members = tuple(sorted(set(variant_ids)))
    if not members:
        raise ValueError("Canonical hypothesis groups require at least one variant")
    return "hypothesis-group:" + _fingerprint(
        {"policy": CANONICAL_HYPOTHESIS_GROUP_POLICY_VERSION, "variant_ids": members}
    )


def research_scope_fingerprint(source_id: str | None) -> str:
    """Return one source-isolated research case scope shared by migration and runtime."""
    return _fingerprint(
        {
            "policy": RESEARCH_SCOPE_POLICY_VERSION,
            "source_id": source_id,
        }
    )


def candidate_semantic_variant(candidate: CandidateThesis) -> CandidateSemanticVariant:
    """Normalize proposal wording into exact, provenance-free economic semantics."""
    capital_kind, capital_reference = _candidate_capital_reference(candidate)
    return CandidateSemanticVariant(
        capital_kind=capital_kind,
        capital_reference=capital_reference,
        direction=candidate.direction,
        horizon_class=candidate.horizon_class,
        theme=_normalized_optional_text(candidate.theme),
        causal_mechanisms=_normalized_text_set(candidate.causal_mechanisms),
        regime_assumptions=_normalized_text_set(candidate.regime_assumptions),
    )


def candidate_review_dimensions(
    left: CandidateSemanticVariant,
    right: CandidateSemanticVariant,
) -> tuple[str, ...]:
    """Return differing fields only when two variants merit human equivalence review."""
    if left.variant_id == right.variant_id:
        return ()
    if (left.capital_kind, left.capital_reference) != (right.capital_kind, right.capital_reference):
        return ()
    if left.direction is not right.direction:
        return ()
    differences: list[str] = []
    for field_name in ("horizon_class", "theme", "causal_mechanisms", "regime_assumptions"):
        if getattr(left, field_name) != getattr(right, field_name):
            differences.append(field_name)
    return tuple(differences)


def research_task_semantics(task: ResearchTaskSemanticInput) -> ResearchTaskSemantics:
    """Remove proposal aliases while retaining the full executable task meaning."""
    return research_task_semantics_from_mapping(
        {
            "provider": task.provider,
            "query": task.query,
            "purpose": task.purpose,
            "maximum_results": task.maximum_results,
            "material_claim_keys": task.material_claim_keys,
        }
    )


def research_task_semantics_from_mapping(value: Mapping[str, object]) -> ResearchTaskSemantics:
    """Project a persisted task payload without importing storage-bearing contracts."""
    provider = value.get("provider")
    query = value.get("query")
    purpose = value.get("purpose")
    maximum_results = value.get("maximum_results", 5)
    claim_keys = value.get("material_claim_keys", ())
    if not isinstance(provider, str) or not isinstance(query, str) or not isinstance(purpose, str):
        raise ValueError("Research task provider, query, and purpose must be strings")
    if not isinstance(maximum_results, int) or isinstance(maximum_results, bool):
        raise ValueError("Research task maximum_results must be an integer")
    if not isinstance(claim_keys, (list, tuple)):
        raise ValueError("Research task material_claim_keys must contain strings")
    untyped_claim_keys = cast("list[object] | tuple[object, ...]", claim_keys)
    if not all(isinstance(item, str) for item in untyped_claim_keys):
        raise ValueError("Research task material_claim_keys must contain strings")
    normalized_claim_keys = cast("list[str] | tuple[str, ...]", untyped_claim_keys)
    return ResearchTaskSemantics(
        provider=_normalized_text(provider),
        query=_normalized_executable_query(query),
        purpose=_normalized_text(purpose),
        material_claim_keys=tuple(sorted(set(normalized_claim_keys))),
        maximum_results=maximum_results,
    )


def research_premise_semantics(
    *,
    hypothesis_id: str,
    material_claims: tuple[CanonicalClaim, ...],
    initial_tasks: tuple[ResearchTaskSemanticInput, ...],
) -> ResearchPremiseSemantics:
    """Build a premise that changes only when material research inputs change."""
    return research_premise_semantics_from_mappings(
        hypothesis_id=hypothesis_id,
        material_claims=tuple(claim.model_dump(mode="json") for claim in material_claims),
        initial_tasks=tuple(
            {
                "provider": task.provider,
                "query": task.query,
                "purpose": task.purpose,
                "maximum_results": task.maximum_results,
                "material_claim_keys": task.material_claim_keys,
            }
            for task in initial_tasks
        ),
    )


def research_premise_semantics_from_mappings(
    *,
    hypothesis_id: str,
    material_claims: tuple[Mapping[str, object], ...],
    initial_tasks: tuple[Mapping[str, object], ...],
) -> ResearchPremiseSemantics:
    """Build the sole premise identity from typed runtime or migrated persisted mappings."""
    claims = tuple(
        sorted(
            (
                {key: value for key, value in claim.items() if key not in {"projected_as_of", "next_refresh_at"}}
                for claim in material_claims
            ),
            key=lambda claim: json.dumps(claim, sort_keys=True, separators=(",", ":"), default=str),
        )
    )
    task_values = tuple(map(research_task_semantics_from_mapping, initial_tasks))
    tasks = tuple(
        sorted(
            {task.model_dump_json(): task for task in task_values}.values(),
            key=lambda task: task.model_dump_json(),
        )
    )
    return ResearchPremiseSemantics(
        hypothesis_id=hypothesis_id,
        material_claims=claims,
        initial_tasks=tasks,
    )


def _candidate_capital_reference(candidate: CandidateThesis) -> tuple[CapitalReferenceKind, str]:
    if candidate.instrument is not None:
        return CapitalReferenceKind.RESOLVED_INSTRUMENT, _normalized_capital_reference(candidate.instrument)
    if candidate.instrument_reference is not None:
        return CapitalReferenceKind.INSTRUMENT_REFERENCE, _normalized_capital_reference(candidate.instrument_reference)
    if candidate.discovery_basis.universe_reference is not None:
        layer = candidate.discovery_basis.universe_layer
        if layer is None:
            raise ValueError("A universe reference requires an exact universe layer")
        return (
            CapitalReferenceKind.UNIVERSE_REFERENCE,
            f"{layer.value}:{_normalized_capital_reference(candidate.discovery_basis.universe_reference)}",
        )
    return CapitalReferenceKind.UNCLASSIFIED_CANDIDATE, candidate.candidate_thesis_id


def _normalized_capital_reference(value: str) -> str:
    normalized = " ".join(_normalized_alphanumeric_tokens(value)).upper()
    if not normalized:
        raise ValueError("Capital reference must not be blank")
    return normalized


def _normalized_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalized_text(value)
    return normalized or None


def _normalized_text(value: str) -> str:
    return " ".join(_normalized_alphanumeric_tokens(value)).casefold()


def _normalized_executable_query(value: str) -> str:
    """Normalize layout without erasing provider query operators or identifier punctuation."""
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("Research task query must not be blank")
    return normalized


def _normalized_text_set(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({_normalized_text(value) for value in values if value.strip()}))


def _normalized_alphanumeric_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).replace("_", " ").replace("-", " ")
    return tuple(
        token
        for raw_token in normalized.split()
        if (token := "".join(character for character in raw_token if character.isalnum()))
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
