"""Module containing the Agent 6 determination contracts — the determination_verdict.json handoff and the determination.json report — for the money_pit package."""

from typing import ClassVar
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import Determination


class DeterminationVerdict(BaseModel):
    """Agent 6 determination output recording the go/no-go handed to the sub-agent and the finalizer.

    Written by the determination node on every outcome, including orchestration failure. The
    finalizer resolves the sub-agent outcome and stamps the run's DeterminationReport from it.
    The slug identifies the run the verdict belongs to, so an artifact left behind by an earlier
    run in the same directory is detectable rather than silently reusable.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    determination: Determination
    reason: str
    failed_steps: list[str]
    sub_agent_spawned: Literal["execution", "notification"] | None


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
