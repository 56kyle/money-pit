"""Module containing provider-free incremental intelligence reports."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.pipeline.chain import PLANNING_CHAIN
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import RunTerminalEvent
from money_pit.schemas.runs import RunTerminalStatus
from money_pit.storage.intelligence_work import RunInferenceUsage
from money_pit.storage.runs import RunRepository


class IntelligenceRunReport(BaseModel):
    """Durable stage progress and provider-reported usage for one run."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    run: RunRecord
    terminal_event: RunTerminalEvent | None
    completed_stages: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    inference: RunInferenceUsage


def intelligence_run_report(
    runs: RunRepository,
    *,
    run_id: str,
    inference: RunInferenceUsage,
) -> IntelligenceRunReport:
    """Build one report from durable records without constructing providers."""
    artifacts = runs.artifacts_for_run(run_id)
    run = runs.get_run(run_id)
    terminal_event = runs.terminal_event_for_run(run_id)
    completed_stages = (
        tuple(stage.value for stage in PLANNING_CHAIN if int(stage.value[1]) <= int(run.through_stage[1]))
        if terminal_event is not None and terminal_event.status is RunTerminalStatus.COMPLETED
        else tuple(artifact.stage for artifact in artifacts)
    )
    return IntelligenceRunReport(
        run=run,
        terminal_event=terminal_event,
        completed_stages=completed_stages,
        artifact_ids=tuple(artifact.artifact_id for artifact in artifacts),
        inference=inference,
    )
