"""Module assembling the capability-scoped persistent A1-A6 harness."""

from itertools import pairwise

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph import START  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph import StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import CompiledStateGraph  # pyright: ignore[reportMissingTypeStubs]

from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.pipeline.chain import PLANNING_CHAIN
from money_pit.pipeline.chain import Stage


_THREAD_ID_CONFIG_KEY = "thread_id"


class ReplayExecutionCapabilityError(Exception):
    """Raised when replay is assembled with an execution-capable A6 node."""


def thread_config(run_id: str) -> RunnableConfig:
    """Address the checkpointed thread for one durable run."""
    return RunnableConfig(configurable={_THREAD_ID_CONFIG_KEY: run_id})


def build_graph(
    *,
    a1: PipelineNode,
    a2: PipelineNode,
    a3: PipelineNode,
    a4: PipelineNode,
    a5: PipelineNode | None,
    a6: PipelineNode | None,
    through: Stage | None = None,
    replay: bool = False,
    replay_node: PipelineNode | None = None,
) -> CompiledStateGraph[PipelineState]:
    """Compile the harness without exposing execution authority to A1-A5."""
    if replay and a6 is not None:
        raise ReplayExecutionCapabilityError("Replay cannot be assembled with an A6 execution capability")
    if replay and replay_node is None:
        raise ReplayExecutionCapabilityError("Replay requires a read-only artifact loader")

    if replay:
        if replay_node is None:
            raise ReplayExecutionCapabilityError("Replay requires a read-only artifact loader")
        resolved_replay_node: PipelineNode = replay_node
        builder: StateGraph[PipelineState] = StateGraph(state_schema=PipelineState)
        _ = builder.add_node("replay", resolved_replay_node)  # pyright: ignore[reportUnknownMemberType]
        _ = builder.add_edge(START, "replay")
        _ = builder.add_edge("replay", END)
        return builder.compile()  # pyright: ignore[reportUnknownMemberType]

    available_nodes: dict[Stage, PipelineNode | None] = {
        Stage.A1: a1,
        Stage.A2: a2,
        Stage.A3: a3,
        Stage.A4: a4,
        Stage.A5: a5,
        Stage.A6: a6,
    }
    terminal = through or Stage.A6
    runnable_chain = PLANNING_CHAIN[: PLANNING_CHAIN.index(terminal) + 1]
    missing = tuple(stage.value for stage in runnable_chain if available_nodes[stage] is None)
    if missing:
        raise ValueError(f"Harness is missing active stage nodes: {missing}")
    nodes: dict[Stage, PipelineNode] = {
        stage: node for stage in runnable_chain if (node := available_nodes[stage]) is not None
    }

    builder = StateGraph(state_schema=PipelineState)
    for stage, node in nodes.items():
        _ = builder.add_node(stage.value, node)  # pyright: ignore[reportUnknownMemberType]

    _ = builder.add_edge(START, Stage.A1.value)
    for current, successor in pairwise(runnable_chain):
        _ = builder.add_edge(current.value, successor.value)
    _ = builder.add_edge(runnable_chain[-1].value, END)

    return builder.compile()  # pyright: ignore[reportUnknownMemberType]
