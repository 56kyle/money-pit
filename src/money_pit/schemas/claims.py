"""Module containing persistent claim contracts for the money_pit package."""

from enum import StrEnum
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class ClaimKind(StrEnum):
    """Kinds of statements retained in claim memory."""

    FACTUAL = "factual"
    FORECAST = "forecast"
    OPINION = "opinion"
    STRATEGY = "strategy"


class ClaimStatus(StrEnum):
    """Lifecycle states of a canonical claim projection."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DISPUTED = "disputed"


class VerificationStatus(StrEnum):
    """Possible outcomes of a bounded claim verification."""

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    MIXED = "mixed"
    UNRESOLVED = "unresolved"


class ClaimObservation(BaseModel):
    """An immutable observation of a claim at a point in time."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(min_length=1)
    canonical_claim_key: str = Field(min_length=1)
    claim_text: str = Field(min_length=1)
    claim_kind: ClaimKind
    subjects: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()
    source_item_id: str = Field(min_length=1)
    evidence_fragment_ids: tuple[str, ...] = ()
    asserted_at: AwareDatetime
    recorded_at: AwareDatetime
    valid_from: AwareDatetime | None = None
    horizon: str | None = None
    expires_at: AwareDatetime | None = None
    supersedes_observation_id: str | None = None


class CanonicalClaim(BaseModel):
    """Current projection derived from immutable claim observations."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    canonical_claim_key: str = Field(min_length=1)
    current_status: ClaimStatus
    active_observation_ids: tuple[str, ...]
    last_material_change_at: AwareDatetime
    next_refresh_at: AwareDatetime | None = None


class VerificationResult(BaseModel):
    """Immutable result of checking one claim observation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    verification_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    status: VerificationStatus
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    checked_at: AwareDatetime
    recorded_at: AwareDatetime
    valid_until: AwareDatetime | None = None
    verifier_version: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()
