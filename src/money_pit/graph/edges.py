"""Module containing deterministic harness routing guards."""

from money_pit.graph.state import PipelineState
from money_pit.pipeline.chain import PLANNING_CHAIN
from money_pit.pipeline.chain import Stage


class StageOrderError(Exception):
    """Raised when a node attempts to run before its predecessor completed."""


def require_predecessor(state: PipelineState, stage: Stage) -> None:
    """Require the immediately preceding persistent stage to be complete."""
    if stage is Stage.A1:
        return
    predecessor_index: int = PLANNING_CHAIN.index(stage) - 1
    predecessor: Stage = PLANNING_CHAIN[predecessor_index]
    if predecessor.value not in state.get("completed_stages", ()):
        raise StageOrderError(f"{stage.value} requires completed stage {predecessor.value}")
