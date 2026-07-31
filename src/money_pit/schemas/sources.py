"""Module containing generic source contracts for the money_pit package."""

from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue


class SourceDefinition(BaseModel):
    """Defines one configured source and its connector."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    adapter_name: str = Field(min_length=1)
    enabled: bool = True
    locator: str = Field(min_length=1)
    cadence: str | None = None
    tags: tuple[str, ...] = ()
    trust_profile: str | None = None
    allowed_uses: tuple[str, ...] = ()
    adapter_config: dict[str, JsonValue] = Field(default_factory=dict)


class SourceRegistryDocument(BaseModel):
    """Versioned top-level shape of a sources.toml file."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    sources: tuple[SourceDefinition, ...] = ()


class SourceCursor(BaseModel):
    """Opaque connector cursor persisted by the source registry."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    value: str


class SourceCursorPurpose(StrEnum):
    """Independent discovery cursor timelines for one configured source."""

    SYNC = "sync"
    BACKFILL = "backfill"


class SourceItem(BaseModel):
    """A stable version of one item discovered from a configured source."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_item_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    canonical_uri: str = Field(min_length=1)
    published_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    discovered_at: AwareDatetime
    content_version: str = Field(min_length=1)


class RawArtifact(BaseModel):
    """Fetched bytes and transport metadata awaiting durable asset storage."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_item: SourceItem
    content: bytes
    media_type: str = Field(min_length=1)
    retrieved_at: AwareDatetime
    canonical_uri: str = Field(min_length=1)
    filename: Path | None = None
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class DiscoveryBatch(BaseModel):
    """One bounded discovery response and the cursor for the next request."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    items: tuple[SourceItem, ...]
    next_cursor: SourceCursor | None = None
    discovered_at: AwareDatetime
