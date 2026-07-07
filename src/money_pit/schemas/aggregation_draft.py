"""Module containing ClaimRelations, the aggregator thin-LLM output of confirmed agree/disagree labels on pre-clustered claim groups, for the money_pit package."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict


class ClaimRelations(BaseModel):
    """Thin-pass LLM confirmation of embedding-clustered corroborations and conflicts."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    agree: list[list[str]]
    disagree: list[list[str]]
