"""StateGraph assembly: add_node / add_edge / add_conditional_edges."""
from collections.abc import Mapping
from typing import Callable

from langgraph.graph import END  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph import START  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph import StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import CompiledStateGraph  # pyright: ignore[reportMissingTypeStubs]

from money_pit.agents.research_tools import DeterministicResearchTools
from money_pit.config import Config
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
from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.answer_draft import AnswerDraft
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.questions import Question
from money_pit.schemas.signals import Claim


def _no_action_terminal(state: PipelineState) -> dict[str, object]:
    result: dict[str, object] = {
        "terminal_state": TerminalState.NO_ACTION,
        "completed_steps": list(state.get("completed_steps") or []) + ["no_action_terminal"],
    }
    return result


def build_graph(
    *,
    fetch_portfolio: Callable[[str], PortfolioSnapshot],
    corroboration_agent: Callable[[list[Claim]], ClaimRelations],
    claim_questions_agent: Callable[[list[Claim]], list[DraftQuestion]],
    answer_synthesis_agent: Callable[[list[Question], list[SourceRef]], list[AnswerDraft]],
    deterministic_tools: DeterministicResearchTools,
    config: Config,
    thesis_agent: Callable[..., AnalysisJudgment],
    place_order: Callable[[ExecutionParameters], str],
    send_email: Callable[[str, str], None],
    manifest: Mapping[str, dict[str, object]] | None = None,
) -> CompiledStateGraph:  # pyright: ignore[reportMissingTypeArgument]
    """Assemble and compile the full money-pit LangGraph pipeline."""
    builder = StateGraph(PipelineState)

    builder.add_node("snapshot", make_snapshot_node(fetch_portfolio=fetch_portfolio))  # pyright: ignore[reportArgumentType]
    builder.add_node("aggregator", make_aggregator_node(corroboration_agent=corroboration_agent))  # pyright: ignore[reportArgumentType]
    builder.add_node("no_action_terminal", _no_action_terminal)
    builder.add_node("questions", make_questions_node(claim_questions_agent=claim_questions_agent))  # pyright: ignore[reportArgumentType]
    builder.add_node("retrieval", make_retrieval_node(answer_synthesis_agent=answer_synthesis_agent, deterministic_tools=deterministic_tools))  # pyright: ignore[reportArgumentType]
    builder.add_node("analysis", make_analysis_node(config=config, thesis_agent=thesis_agent))  # pyright: ignore[reportArgumentType]
    builder.add_node("validator", make_validator_node(manifest=manifest))  # pyright: ignore[reportArgumentType]
    builder.add_node("determination", make_determination_node())  # pyright: ignore[reportArgumentType]
    builder.add_node("execution", make_execution_node(place_order=place_order))  # pyright: ignore[reportArgumentType]
    builder.add_node("notification", make_notification_node(send_email=send_email))  # pyright: ignore[reportArgumentType]
    builder.add_node("finalizer", make_finalizer_node())  # pyright: ignore[reportArgumentType]

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
