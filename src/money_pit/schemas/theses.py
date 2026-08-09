"""Module containing candidate and append-only investment-thesis contracts."""

import math
from enum import StrEnum
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.universe import DiscoveryBasis


class ThesisDirection(StrEnum):
    """Directional posture of an investment hypothesis."""

    LONG = "long"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class ThesisStatus(StrEnum):
    """Lifecycle states of a thesis revision."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    WEAKENED = "weakened"
    INVALIDATED = "invalidated"
    CLOSED = "closed"


class CandidateStatus(StrEnum):
    """Research lifecycle of a candidate hypothesis."""

    OPEN = "open"
    RESEARCHING = "researching"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class ScenarioOutcome(BaseModel):
    """One scenario in a normalized return distribution."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1)
    probability: float = Field(ge=0, le=1)
    expected_return: float

    @model_validator(mode="after")
    def validate_return(self) -> Self:
        """Reject non-finite and economically impossible long-only loss values."""
        if not math.isfinite(self.expected_return) or self.expected_return < -1:
            raise ValueError("scenario expected return must be finite and at least -1")
        return self


class CandidateThesis(BaseModel):
    """An unpromoted, sourced investment hypothesis."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    candidate_thesis_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    direction: ThesisDirection
    instrument_reference: str | None = Field(default=None, min_length=1)
    instrument: str | None = Field(default=None, min_length=1)
    theme: str | None = Field(default=None, min_length=1)
    horizon_class: HorizonClass
    discovery_basis: DiscoveryBasis
    causal_mechanisms: tuple[str, ...] = ()
    regime_assumptions: tuple[str, ...] = ()
    status: CandidateStatus = CandidateStatus.OPEN
    created_at: AwareDatetime
    known_at: AwareDatetime


class ThesisRevision(BaseModel):
    """A complete immutable version of one investment thesis."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    revision_id: str = Field(min_length=1)
    thesis_id: str = Field(min_length=1)
    revision_number: int = Field(ge=1)
    promoted_from_candidate_id: str | None = Field(default=None, min_length=1)
    subject: str = Field(min_length=1)
    instrument: str | None = Field(default=None, min_length=1)
    theme: str | None = Field(default=None, min_length=1)
    direction: ThesisDirection
    status: ThesisStatus
    horizon_class: HorizonClass
    effective_from: AwareDatetime
    event_at: AwareDatetime | None = None
    review_at: AwareDatetime
    valid_until: AwareDatetime | None = None
    scenario_distribution: tuple[ScenarioOutcome, ...] = Field(min_length=1)
    invalidation_rules: tuple[str, ...] = Field(min_length=1)
    supporting_claim_keys: tuple[str, ...] = ()
    contradicting_claim_keys: tuple[str, ...] = ()
    causal_mechanisms: tuple[str, ...] = Field(min_length=1)
    regime_assumptions: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(min_length=1)
    created_at: AwareDatetime
    known_at: AwareDatetime

    @model_validator(mode="after")
    def validate_revision(self) -> Self:
        """Require normalized scenarios and ordered economic bounds."""
        probability_total: float = sum(scenario.probability for scenario in self.scenario_distribution)
        if abs(probability_total - 1.0) > 1e-9:
            raise ValueError("scenario_distribution probabilities must sum to 1.")
        names = tuple(item.name.strip().casefold() for item in self.scenario_distribution)
        if len(names) != len(set(names)):
            raise ValueError("scenario_distribution names must be unique")
        if self.review_at < self.effective_from:
            raise ValueError("review_at must not precede effective_from.")
        if self.valid_until is not None and self.valid_until < self.effective_from:
            raise ValueError("valid_until must not precede effective_from.")
        if self.revision_number == 1 and self.promoted_from_candidate_id is None:
            raise ValueError("The first thesis revision must identify its promoted candidate.")
        if self.revision_number > 1 and self.promoted_from_candidate_id is not None:
            raise ValueError("Only the first thesis revision may identify a promoted candidate.")
        return self
