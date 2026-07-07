"""Module containing DeterminationReport, the determination.json contract produced by the Agent 6 finalizer node, for the money_pit package."""

from typing import ClassVar
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import Determination


class DeterminationReport(BaseModel):
    """Agent 6 finalizer output summarising the PROCEED/HALT decision and the sub-agent outcome."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    determination: Determination
    reason: str
    failed_steps: list[str]
    sub_agent_spawned: Literal["execution", "notification"] | None
    sub_agent_outcome: Literal["success", "failure"] | None
    sub_agent_error: str | None
    timestamp: str
