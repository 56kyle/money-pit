"""Module orchestrating one injected persistent A1-A6 harness run."""

from dataclasses import dataclass
from typing import cast

from money_pit.graph.graph import build_graph
from money_pit.graph.graph import thread_config
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_requested_as_of
from money_pit.graph.state import require_run_id
from money_pit.graph.state import require_run_started_at
from money_pit.pipeline.chain import Stage
from money_pit.schemas.runs import RunRecord


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
