"""Module containing append-only claim-memory contracts."""

from enum import StrEnum
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel


class HorizonClass(StrEnum):
    """Versioned economic horizon classes."""

    EVENT = "event"
    TACTICAL = "tactical"
    MEDIUM_TERM = "medium_term"
    STRUCTURAL = "structural"


class ClaimKind(StrEnum):
    """Kinds of statements retained in claim memory."""

    FACTUAL = "factual"
    FORECAST = "forecast"
    OPINION = "opinion"
    STRATEGY = "strategy"


class ClaimCategory(StrEnum):
    """Economic categories used for freshness and verification policy."""

    FUNDAMENTAL = "fundamental"
    TECHNICAL = "technical"
    MACRO = "macro"
    SENTIMENT = "sentiment"
    CATALYST = "catalyst"
    MARKET = "market"
    PORTFOLIO = "portfolio"


class ClaimStatus(StrEnum):
    """Lifecycle states of a deterministic canonical projection."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DISPUTED = "disputed"


class ClaimResolutionKind(StrEnum):
    """Relations supported by canonical claim resolution."""

    SAME = "same"
    CONTRADICTS = "contradicts"
    DISTINCT = "distinct"
    UPDATES = "updates"


class VerificationStatus(StrEnum):
    """Possible outcomes of bounded verification."""

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    MIXED = "mixed"
    UNRESOLVED = "unresolved"


class ClaimObservation(BaseModel):
    """An immutable source interpretation known at a specific time."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(min_length=1)
    claim_text: str = Field(min_length=1)
    claim_kind: ClaimKind
    category: ClaimCategory
    source_item_id: str = Field(min_length=1)
    evidence_fragment_ids: tuple[str, ...] = Field(min_length=1)
    asserted_at: AwareDatetime
    known_at: AwareDatetime
    effective_from: AwareDatetime | None = None
    event_at: AwareDatetime | None = None
    review_at: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    horizon_class: HorizonClass
    instruments: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()
    causal_mechanisms: tuple[str, ...] = ()
    regime_assumptions: tuple[str, ...] = ()
    supersedes_observation_id: str | None = None

    @model_validator(mode="after")
    def validate_economic_interval(self) -> Self:
        """Require review and validity bounds to follow economic effectiveness."""
        if self.effective_from is not None:
            if self.review_at is not None and self.review_at < self.effective_from:
                raise ValueError("review_at must not precede effective_from.")
            if self.valid_until is not None and self.valid_until < self.effective_from:
                raise ValueError("valid_until must not precede effective_from.")
        return self


class UnresolvedObservationCursor(BaseModel):
    """Stable keyset cursor for bounded unresolved-observation scans."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    known_at: AwareDatetime
    asserted_at: AwareDatetime
    observation_id: str = Field(min_length=1)


class UnresolvedObservationPage(BaseModel):
    """One bounded deterministic page of unresolved claim observations."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    items: tuple[ClaimObservation, ...]
    next_cursor: UnresolvedObservationCursor | None = None


class ClaimResolutionDecision(BaseModel):
    """An immutable decision accepting a claim or relating two observations."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    decision_id: str = Field(min_length=1)
    subject_observation_id: str = Field(min_length=1)
    object_observation_id: str | None = Field(default=None, min_length=1)
    relation: ClaimResolutionKind
    decided_at: AwareDatetime
    known_at: AwareDatetime
    resolver_version: str = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_actual_times(self) -> Self:
        """Require availability to follow the actual resolution decision."""
        if self.known_at < self.decided_at:
            raise ValueError("known_at must not precede decided_at.")
        if self.relation is ClaimResolutionKind.DISTINCT:
            if self.object_observation_id is not None:
                raise ValueError("A distinct resolution must be unary and have no object observation.")
        elif self.object_observation_id is None:
            raise ValueError("A non-distinct resolution requires an object observation.")
        elif self.object_observation_id == self.subject_observation_id:
            raise ValueError("A non-distinct resolution requires two different observations.")
        return self


class VerificationResult(BaseModel):
    """An immutable result of checking one observation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    verification_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    status: VerificationStatus
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    independent_provenance_groups: tuple[str, ...] = ()
    checked_at: AwareDatetime
    known_at: AwareDatetime
    valid_until: AwareDatetime | None = None
    verifier_version: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_actual_times(self) -> Self:
        """Require availability to follow the actual verification check."""
        if self.known_at < self.checked_at:
            raise ValueError("known_at must not precede checked_at.")
        return self


class VerificationEvidenceAuthority(BaseModel):
    """Exact point-in-time source policy authorizing one verification fragment."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    fragment_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    source_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_group: str = Field(min_length=1)
    trust_category: TrustCategory
    trust_level: TrustLevel
    allowed_uses: tuple[AllowedUse, ...] = Field(min_length=1)


class CanonicalClaim(BaseModel):
    """Deterministic current or point-in-time claim projection."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    canonical_claim_key: str = Field(min_length=1)
    current_status: ClaimStatus
    active_observation_ids: tuple[str, ...]
    projected_as_of: AwareDatetime
    last_material_change_at: AwareDatetime
    next_refresh_at: AwareDatetime | None = None
    freshness_policy_version: str = Field(min_length=1)
