"""Module containing evidence provenance contracts for the money_pit package."""

from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from typing import Literal
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class TimestampLocator(BaseModel):
    """Locates evidence within time-based media."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["timestamp"] = "timestamp"
    start_seconds: float = Field(ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    bounding_box: tuple[float, float, float, float] | None = None


class PageLocator(BaseModel):
    """Locates evidence within a paginated document."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["page"] = "page"
    page_number: int = Field(ge=1)
    bounding_box: tuple[float, float, float, float] | None = None


class TextLocator(BaseModel):
    """Locates evidence within extracted text."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["text"] = "text"
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)


EvidenceLocator = TimestampLocator | PageLocator | TextLocator


class EvidenceAsset(BaseModel):
    """An immutable content-addressed source asset."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    asset_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    local_path: Path
    retrieved_at: AwareDatetime

    @model_validator(mode="after")
    def validate_content_address(self) -> Self:
        """Require the durable asset identifier to be its content digest."""
        if self.asset_id != self.content_hash:
            raise ValueError("asset_id must equal content_hash.")
        return self


class EvidenceFragment(BaseModel):
    """A traceable fragment extracted from an evidence asset."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    fragment_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    kind: Literal["transcript", "frame", "page", "web_span", "table"]
    locator: EvidenceLocator
    extracted_text: str | None = None
    cited_source_text: str | None = None
    extraction_method: str = Field(min_length=1)
    extraction_model: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class EvidenceDocument(BaseModel):
    """Extracted evidence whose fragments retain links to the original asset."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    asset: EvidenceAsset
    fragments: tuple[EvidenceFragment, ...]


class EvidenceProcessingStatus(StrEnum):
    """Outcome of one evidence-processing attempt."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EvidenceProcessingAttempt(BaseModel):
    """Durable record of one processor invocation against an acquisition."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    attempt_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    content_version: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    processor_name: str = Field(min_length=1)
    processor_version: str = Field(min_length=1)
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    status: EvidenceProcessingStatus
    failure_kind: str | None = Field(default=None, min_length=1)
    document_id: str | None = Field(default=None, min_length=1)
    fragment_ids: tuple[str, ...] = ()
