"""Module containing the Claim, SignalSet, CorroborationEntry, and AggregatedSignals post-conversion contracts for the money_pit package."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import ClaimRelationType
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.provenance import SourceRef


class Claim(BaseModel):
    """A classified investment claim from a single source adapter."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    tier: SignalTier
    claim: str
    category: ClaimCategory
    tickers_affected: list[str]
    requires_validation: bool
    source_ref: SourceRef
    cited_sources: list[str]


class SignalSet(BaseModel):
    """Normalized output of one source adapter; written to signals/{source_id}.json."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    source_ref: SourceRef
    summary: str
    claims: list[Claim]
    tickers_mentioned: list[str]
    sectors_mentioned: list[str]
    macro_themes: list[str]
    has_actionable_content: bool


class CorroborationEntry(BaseModel):
    """A confirmed agree or disagree relation across a cluster of claims."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    relation: ClaimRelationType
    claim_ids: list[str]


class AggregatedSignals(BaseModel):
    """Union of all per-source SignalSets for a run; written to aggregated_signals.json."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    sources: list[SourceRef]
    claims: list[Claim]
    corroborations: list[CorroborationEntry]
    conflicts: list[CorroborationEntry]
    has_actionable_content: bool
