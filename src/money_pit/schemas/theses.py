"""Module containing living investment thesis contracts for the money_pit package."""

from enum import StrEnum
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class ThesisDirection(StrEnum):
    """Directional posture of an investment thesis."""

    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class ThesisStatus(StrEnum):
    """Lifecycle states of a living thesis."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    WEAKENED = "weakened"
    INVALIDATED = "invalidated"
    CLOSED = "closed"


class ScenarioOutcome(BaseModel):
    """One normalized scenario in a thesis distribution."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    probability: float = Field(ge=0, le=1)
    expected_return: float


class Thesis(BaseModel):
    """A persistent investment judgment linked to claims."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    thesis_id: str = Field(min_length=1)
    instrument: str | None = None
    theme: str | None = None
    direction: ThesisDirection
    horizon: str = Field(min_length=1)
    status: ThesisStatus
    supporting_claim_keys: tuple[str, ...] = ()
    contradicting_claim_keys: tuple[str, ...] = ()
    scenario_distribution: tuple[ScenarioOutcome, ...] = ()
    invalidation_rules: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    created_at: AwareDatetime
    reviewed_at: AwareDatetime
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_review_order(self) -> Self:
        """Require reviews to occur no earlier than thesis creation."""
        if self.reviewed_at < self.created_at:
            raise ValueError("reviewed_at must be at or after created_at.")
        return self
