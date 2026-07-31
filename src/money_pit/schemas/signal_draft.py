"""Module containing SignalSetDraft, the raw A1 LLM output before ticker normalization, date parsing, and schema validation, for the money_pit package."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class ClaimDraft(BaseModel):
    """A single claim as the LLM emitted it, before adapter normalization."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    claim: str
    tier: str
    category: str
    tickers_affected: list[str]
    cited_sources: list[str]
    evidence_fragment_ids: list[str] = Field(default_factory=list)


class SignalSetDraft(BaseModel):
    """Raw A1 LLM output; the adapter post-processor converts this to SignalSet."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    source_type: str
    title: str
    url: str | None
    published_at: str | None
    retrieved_at: str
    summary: str
    claims: list[ClaimDraft]
    tickers_mentioned: list[str]
    sectors_mentioned: list[str]
    macro_themes: list[str]
