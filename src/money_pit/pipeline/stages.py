"""Module invoking one already-composed persistent harness stage."""

from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_requested_as_of
from money_pit.graph.state import require_run_id
from money_pit.graph.state import require_run_started_at
from money_pit.pipeline.chain import Stage


class StageInvocationError(Exception):
    """Raised when a composed node does not record its requested stage."""


def run_stage(stage: Stage, node: PipelineNode, state: PipelineState) -> PipelineState:
    """Invoke one injected node without constructing unrelated capabilities."""
    _ = require_run_id(state)
    _ = require_requested_as_of(state)
    _ = require_run_started_at(state)
    update: PipelineState = node(state)
    if stage.value not in update.get("completed_stages", ()):
        raise StageInvocationError(f"{stage.value} node did not record stage completion")
    return {**state, **update}
