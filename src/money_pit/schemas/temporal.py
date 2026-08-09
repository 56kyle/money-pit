"""Module containing deterministic temporal-synthesis contracts."""

from enum import StrEnum
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class SignalContributionKind(StrEnum):
    """Relations between a claim observation and thesis revision."""

    REINFORCES = "reinforces"
    CONTRADICTS = "contradicts"
    UPDATES = "updates"
    INDEPENDENT = "independent"
    NOT_COMPARABLE = "not_comparable"


class CausalBridgeKind(StrEnum):
    """Configured relationship types that can bridge otherwise separate signals."""

    LEADING_INDICATOR = "leading_indicator"
    TRANSMISSION = "transmission"
    UPDATE = "update"
    PROXY = "proxy"


class CausalBridge(BaseModel):
    """Typed directional bridge proposed by synthesis and checked deterministically."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    kind: CausalBridgeKind
    source_subject: str = Field(min_length=1)
    target_subject: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class SignalContribution(BaseModel):
    """A traceable temporal and economic comparison result."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    contribution_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    thesis_revision_id: str = Field(min_length=1)
    relation: SignalContributionKind
    temporal_compatible: bool
    compatibility_reasons: tuple[str, ...] = Field(min_length=1)
    causal_bridge: CausalBridge | None = None
    judged_at: AwareDatetime
    known_at: AwareDatetime
    synthesis_version: str = Field(min_length=1)
