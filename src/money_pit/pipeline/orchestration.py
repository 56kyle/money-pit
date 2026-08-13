"""Module orchestrating one injected persistent A1-A6 harness run."""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar
from typing import cast

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.graph.graph import build_graph
from money_pit.graph.graph import thread_config
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_requested_as_of
from money_pit.graph.state import require_run_id
from money_pit.graph.state import require_run_started_at
from money_pit.pipeline.chain import Stage
from money_pit.schemas.runs import RunRecord
from money_pit.storage.intelligence_work import IntelligenceWorkCompletionCounts
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import IntelligenceWorkStatus
from money_pit.storage.intelligence_work import RunInferenceUsage


class IntelligenceStage(StrEnum):
    """Operator-facing terminal stage for one bounded intelligence update."""

    INTERPRETATION = "interpretation"
    DISCOVERY = "discovery"
    RESEARCH = "research"
    SYNTHESIS = "synthesis"

    def pipeline_stage(self) -> Stage:
        """Map the semantic stage to its internal graph identity."""
        return {
            IntelligenceStage.INTERPRETATION: Stage.A1,
            IntelligenceStage.DISCOVERY: Stage.A2,
            IntelligenceStage.RESEARCH: Stage.A3,
            IntelligenceStage.SYNTHESIS: Stage.A4,
        }[self]


class IntelligenceUpdateReport(BaseModel):
    """Durable progress and provider-reported usage for one update."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    run_id: str
    through: IntelligenceStage
    completed_stages: tuple[str, ...]
    completed: IntelligenceWorkCompletionCounts
    usage: RunInferenceUsage
    remaining: IntelligenceWorkStatus


@dataclass(frozen=True)
class HarnessNodes:
    """Capability-scoped stage nodes supplied by application composition."""

    a1: PipelineNode
    a2: PipelineNode | None
    a3: PipelineNode | None
    a4: PipelineNode | None
    a5: PipelineNode | None
    a6: PipelineNode | None
    replay: PipelineNode | None = None


@dataclass(frozen=True)
class IntelligenceNodes:
    """A1-A4 capabilities available to incremental intelligence updates."""

    a1: PipelineNode
    a2: PipelineNode | None = None
    a3: PipelineNode | None = None
    a4: PipelineNode | None = None


def run_intelligence_update(
    initial_state: PipelineState,
    *,
    nodes: IntelligenceNodes,
    through: IntelligenceStage,
    run_record: RunRecord,
    work: IntelligenceWorkRepository,
) -> IntelligenceUpdateReport:
    """Run one bounded intelligence update and report durable continuation work."""
    pipeline_nodes = HarnessNodes(
        a1=nodes.a1,
        a2=nodes.a2,
        a3=nodes.a3,
        a4=nodes.a4,
        a5=None,
        a6=None,
    )
    source_id = initial_state.get("source_id")
    final_state = run_pipeline(
        initial_state,
        nodes=pipeline_nodes,
        through=through.pipeline_stage(),
        run_record=run_record,
    )
    run_id = require_run_id(final_state)
    remaining = work.status(source_id)
    return IntelligenceUpdateReport(
        run_id=run_id,
        through=through,
        completed_stages=final_state.get("completed_stages", ()),
        completed=work.completion_counts_for_run(run_id),
        usage=work.usage_for_run(run_id),
        remaining=remaining,
    )


def run_pipeline(
    initial_state: PipelineState,
    *,
    nodes: HarnessNodes,
    through: Stage | None = None,
    replay: bool = False,
    run_record: RunRecord,
) -> PipelineState:
    """Run persistent intelligence after composition has durably registered its start."""
    run_id: str = require_run_id(initial_state)
    requested_as_of = require_requested_as_of(initial_state)
    started_at = require_run_started_at(initial_state)
    terminal_stage = Stage(run_record.through_stage) if replay and through is None else through or Stage.A6
    if (
        run_record.run_id != run_id
        or run_record.requested_as_of != requested_as_of
        or run_record.started_at != started_at
        or run_record.through_stage != terminal_stage.value
    ):
        raise ValueError("RunRecord does not match the requested harness invocation")
    graph = build_graph(
        a1=nodes.a1,
        a2=nodes.a2,
        a3=nodes.a3,
        a4=nodes.a4,
        a5=nodes.a5,
        a6=nodes.a6,
        through=through,
        replay=replay,
        replay_node=nodes.replay,
    )
    state: PipelineState = {**initial_state, "replay": replay}
    return cast(
        "PipelineState",
        graph.invoke(state, config=thread_config(run_id)),  # pyright: ignore[reportUnknownMemberType]
    )
