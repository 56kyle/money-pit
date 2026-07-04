"""SourceRef — structured provenance record for every pipeline signal source."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import SourceType


class SourceRef(BaseModel):
    """Identifies and locates the source that delivered a claim."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    source_type: SourceType
    title: str
    url: str | None
    published_at: str | None
    retrieved_at: str
    locator: str | None
