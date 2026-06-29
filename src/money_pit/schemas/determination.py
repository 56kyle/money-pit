"""DeterminationReport — determination.json contract produced by the Agent 6 conditional edge."""
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from money_pit.schemas.enums import Determination


class DeterminationReport(BaseModel):
    """Agent 6 (or conditional edge) output summarising the PROCEED/HALT decision and sub-agent outcome."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    determination: Determination
    reason: str
    failed_steps: list[str]
    sub_agent_spawned: str | None
    sub_agent_outcome: str | None
    sub_agent_error: str | None
    timestamp: str
