"""StateGraph assembly: add_node / add_edge / add_conditional_edges."""

from pathlib import Path

from langgraph.graph import END  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph import START  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph import StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import CompiledStateGraph  # pyright: ignore[reportMissingTypeStubs]

from money_pit.agents.research_tools import DeterministicResearchTools
from money_pit.config import Config
from money_pit.contracts import AnswerSynthesisAgent
from money_pit.contracts import ClaimQuestionsAgent
from money_pit.contracts import CorroborationAgent
from money_pit.contracts import EmailSender
from money_pit.contracts import OrderPlacer
from money_pit.contracts import PortfolioFetcher
from money_pit.contracts import ThesisAgent
from money_pit.contracts import ToolManifest
from money_pit.graph.edges import EXECUTE
from money_pit.graph.edges import FINALIZE
from money_pit.graph.edges import NO_ACTION
from money_pit.graph.edges import NOTIFY
from money_pit.graph.edges import PROCEED
from money_pit.graph.edges import TERMINATE
from money_pit.graph.edges import VALIDATE
from money_pit.graph.edges import determination_router
from money_pit.graph.edges import post_notification_router
from money_pit.graph.edges import signal_gate
from money_pit.graph.edges import terminal_state_router
from money_pit.graph.state import PipelineState
from money_pit.pipeline.aggregator import make_aggregator_node
from money_pit.pipeline.analysis import make_analysis_node
from money_pit.pipeline.determination import make_determination_node
from money_pit.pipeline.determination import make_finalizer_node
from money_pit.pipeline.execution import make_execution_node
from money_pit.pipeline.notification import make_notification_node
from money_pit.pipeline.questions import make_questions_node
from money_pit.pipeline.retrieval import make_retrieval_node
from money_pit.pipeline.snapshot import make_snapshot_node
from money_pit.pipeline.validator import make_validator_node
from money_pit.schemas.enums import TerminalState


def _no_action_terminal(state: PipelineState) -> PipelineState:
    result: PipelineState = {
        "terminal_state": TerminalState.NO_ACTION,
        "completed_steps": list(state.get("completed_steps") or []) + ["no_action_terminal"],
    }
    return result


def build_graph(
    *,
    fetch_portfolio: PortfolioFetcher,
    corroboration_agent: CorroborationAgent,
    claim_questions_agent: ClaimQuestionsAgent,
    answer_synthesis_agent: AnswerSynthesisAgent,
    deterministic_tools: DeterministicResearchTools,
    config: Config,
    thesis_agent: ThesisAgent,
    place_order: OrderPlacer,
    send_email: EmailSender,
    manifest: ToolManifest | None = None,
    order_schema_path: Path | None = None,
) -> CompiledStateGraph[PipelineState]:
    """Assemble and compile the full money-pit LangGraph pipeline."""
    builder: StateGraph[PipelineState] = StateGraph(state_schema=PipelineState)

    builder.add_node("snapshot", make_snapshot_node(fetch_portfolio=fetch_portfolio))
    builder.add_node("aggregator", make_aggregator_node(corroboration_agent=corroboration_agent))
    builder.add_node("no_action_terminal", _no_action_terminal)
    builder.add_node("questions", make_questions_node(claim_questions_agent=claim_questions_agent))
    builder.add_node(
        "retrieval",
        make_retrieval_node(answer_synthesis_agent=answer_synthesis_agent, deterministic_tools=deterministic_tools),
    )
    builder.add_node(
        "analysis",
        make_analysis_node(config=config, thesis_agent=thesis_agent, order_schema_path=order_schema_path),
    )
    builder.add_node("validator", make_validator_node(manifest=manifest))
    builder.add_node("determination", make_determination_node())
    builder.add_node("execution", make_execution_node(place_order=place_order))
    builder.add_node("notification", make_notification_node(send_email=send_email))
    builder.add_node("finalizer", make_finalizer_node())

    builder.add_edge(START, "snapshot")
    builder.add_edge("snapshot", "aggregator")
    builder.add_conditional_edges("aggregator", signal_gate, {PROCEED: "questions", NO_ACTION: "no_action_terminal"})
    builder.add_edge("no_action_terminal", END)
    builder.add_edge("questions", "retrieval")
    builder.add_edge("retrieval", "analysis")
    builder.add_conditional_edges(
        "analysis",
        terminal_state_router,
        {VALIDATE: "validator", NO_ACTION: "no_action_terminal", NOTIFY: "notification"},
    )
    builder.add_edge("validator", "determination")
    builder.add_conditional_edges(
        "determination",
        determination_router,
        {EXECUTE: "execution", NOTIFY: "notification", FINALIZE: "finalizer"},
    )
    builder.add_edge("execution", "finalizer")
    builder.add_conditional_edges(
        "notification",
        post_notification_router,
        {TERMINATE: END, FINALIZE: "finalizer"},
    )
    builder.add_edge("finalizer", END)

    return builder.compile()
