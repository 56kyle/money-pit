from datetime import UTC
from datetime import datetime

import pytest

from money_pit.graph.edges import StageOrderError
from money_pit.graph.edges import require_predecessor
from money_pit.graph.graph import ReplayExecutionCapabilityError
from money_pit.graph.graph import build_graph
from money_pit.graph.state import PipelineState
from money_pit.graph.state import PipelineStateError
from money_pit.graph.state import completed_with
from money_pit.graph.state import require_requested_as_of
from money_pit.pipeline.chain import Stage


def _node(state: PipelineState) -> PipelineState:
    return state


def test_build_graph_compiles_every_stage_without_invoking_nodes() -> None:
    compiled = build_graph(a1=_node, a2=_node, a3=_node, a4=_node, a5=_node, a6=_node)

    assert {stage.value for stage in Stage} <= set(compiled.nodes)


def test_build_graph_replay_rejects_broker_execution_capability() -> None:
    with pytest.raises(ReplayExecutionCapabilityError):
        _ = build_graph(a1=_node, a2=_node, a3=_node, a4=_node, a5=_node, a6=_node, replay=True)


def test_build_graph_replay_compiles_without_a6() -> None:
    compiled = build_graph(
        a1=_node,
        a2=_node,
        a3=_node,
        a4=_node,
        a5=_node,
        a6=None,
        replay=True,
        replay_node=_node,
    )

    assert set(compiled.nodes) == {"__start__", "replay"}


def test_require_predecessor_fails_closed_for_out_of_order_stage() -> None:
    with pytest.raises(StageOrderError):
        require_predecessor({}, Stage.A4)


def test_require_requested_as_of_rejects_a_naive_cutoff() -> None:
    with pytest.raises(PipelineStateError):
        _ = require_requested_as_of({"requested_as_of": datetime(2026, 8, 1)})  # noqa: DTZ001


def test_completed_with_is_idempotent() -> None:
    state: PipelineState = {
        "requested_as_of": datetime(2026, 8, 1, tzinfo=UTC),
        "completed_stages": (Stage.A1.value,),
    }

    assert completed_with(state, Stage.A1.value) == (Stage.A1.value,)
