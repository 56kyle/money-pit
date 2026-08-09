"""Module containing persistent source and acquisition contracts."""

from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator


class AllowedUse(StrEnum):
    """Purposes for which source material may be used."""

    INTERPRETATION = "interpretation"
    THESIS_GENERATION = "thesis_generation"
    FACTUAL_VERIFICATION = "factual_verification"
    MARKET_DATA = "market_data"
    PORTFOLIO_DATA = "portfolio_data"
    PORTFOLIO_DECISION = "portfolio_decision"


class TrustCategory(StrEnum):
    """Evidence categories with independently configured trust."""

    FACTUAL = "factual"
    FORECAST = "forecast"
    OPINION = "opinion"
    STRATEGY = "strategy"
    MARKET = "market"
    PORTFOLIO = "portfolio"


class TrustLevel(StrEnum):
    """Configured authority of a source for one evidence category."""

    AUTHORITATIVE_PRIMARY = "authoritative_primary"
    INDEPENDENT_SECONDARY = "independent_secondary"
    COMMENTARY = "commentary"
    UNTRUSTED = "untrusted"


class SourceTrustSetting(BaseModel):
    """Trust assigned to one source for one evidence category."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    category: TrustCategory
    level: TrustLevel


class SourceDefinition(BaseModel):
    """One immutable revision of a configured source."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    source_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    adapter_name: str = Field(min_length=1)
    enabled: bool = True
    locator: str = Field(min_length=1)
    provenance_group: str = Field(min_length=1)
    allowed_uses: tuple[AllowedUse, ...] = Field(min_length=1)
    trust_settings: tuple[SourceTrustSetting, ...] = Field(min_length=1)
    cadence: str | None = None
    tags: tuple[str, ...] = ()
    adapter_config: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_policy_categories(self) -> Self:
        """Require at most one trust decision for each source category."""
        categories: tuple[TrustCategory, ...] = tuple(setting.category for setting in self.trust_settings)
        if len(categories) != len(set(categories)):
            raise ValueError("trust_settings must contain each category at most once.")
        return self


class SourceRegistryDocument(BaseModel):
    """Versioned top-level shape of a sources.toml file."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    version: str = Field(min_length=1)
    sources: tuple[SourceDefinition, ...] = ()


class SourceCursor(BaseModel):
    """Opaque connector cursor persisted for one timeline."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    value: str


class SourceCursorPurpose(StrEnum):
    """Independent discovery cursor timelines."""

    SYNC = "sync"
    BACKFILL = "backfill"


class SourceItem(BaseModel):
    """A stable version of an item discovered from one source revision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    source_item_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_definition_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    canonical_uri: str = Field(min_length=1)
    published_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    discovered_at: AwareDatetime
    content_version: str = Field(min_length=1)


class RawArtifact(BaseModel):
    """Fetched bytes and transport metadata awaiting durable storage."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    source_item: SourceItem
    content: bytes
    media_type: str = Field(min_length=1)
    retrieved_at: AwareDatetime
    canonical_uri: str = Field(min_length=1)
    filename: Path | None = None
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class DiscoveryBatch(BaseModel):
    """One bounded source discovery response."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    items: tuple[SourceItem, ...]
    next_cursor: SourceCursor | None = None
    discovered_at: AwareDatetime
