"""SignalSetDraft — raw A1 LLM output before ticker normalization, date parsing, and schema validation."""
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class ClaimDraft(BaseModel):
    """A single claim as the LLM emitted it, before adapter normalization."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    claim: str
    source_context: str
    tier: str
    category: str
    tickers_affected: list[str]
    requires_validation: bool
    cited_sources: list[str]


class SignalSetDraft(BaseModel):
    """Raw A1 LLM output; the adapter post-processor converts this to SignalSet."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    source_type: str
    title: str
    url: str | None
    published_at: str | None
    retrieved_at: str
    episode_summary: str
    claims: list[ClaimDraft]
    tickers_mentioned: list[str]
    sectors_mentioned: list[str]
    macro_themes: list[str]
    # LLM-emitted; overridden deterministically by compute/signal_flags.has_actionable_content
    has_actionable_content: bool
