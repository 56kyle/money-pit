"""ClaimRelations — aggregator thin LLM output: confirmed agree/disagree labels on pre-clustered claim groups."""
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class ClaimRelations(BaseModel):
    """Thin-pass LLM confirmation of embedding-clustered corroborations and conflicts."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    agree: list[list[str]]
    disagree: list[list[str]]
