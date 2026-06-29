"""StateGraph assembly: add_node / add_edge / add_conditional_edges."""
from typing import Callable

from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import CompiledStateGraph  # pyright: ignore[reportMissingTypeStubs]

from money_pit.agents.research_tools import DeterministicResearchTools
from money_pit.config import Config
from money_pit.graph.edges import (
    EXECUTE,
    HALT,
    NO_ACTION,
    NOTIFY,
    PROCEED,
    VALIDATE,
    determination_gate,
    signal_gate,
    terminal_state_router,
)
from money_pit.graph.state import PipelineState
from money_pit.pipeline.aggregator import make_aggregator_node
from money_pit.pipeline.analysis import make_analysis_node
from money_pit.pipeline.execution import make_execution_node
from money_pit.pipeline.notification import make_notification_node
from money_pit.pipeline.questions import make_questions_node
from money_pit.pipeline.retrieval import make_retrieval_node
from money_pit.pipeline.snapshot import make_snapshot_node
from money_pit.pipeline.validator import make_validator_node
from money_pit.schemas.action_steps import ActionStep, ExecutionParameters
from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.answers import Answer, InitialAnswers
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.questions import Question
from money_pit.schemas.signals import AggregatedSignals, Claim


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
    claim_questions_agent: Callable[[list[Claim]], list[Question]],
    answer_synthesis_agent: Callable[[list[Question], list[SourceRef]], list[Answer]],
    deterministic_tools: DeterministicResearchTools,
    config: Config,
    thesis_agent: Callable[[AggregatedSignals, PortfolioSnapshot, InitialAnswers], list[AnalysisJudgment]],
    behavioral_match_agent: Callable[[ActionStep, str], bool],
    place_order: Callable[[ExecutionParameters], str],
    send_email: Callable[[str, str], None],
) -> CompiledStateGraph:  # pyright: ignore[reportMissingTypeArgument]
    """Assemble and compile the full money-pit LangGraph pipeline."""
    builder = StateGraph(PipelineState)

    builder.add_node("snapshot", make_snapshot_node(fetch_portfolio=fetch_portfolio))  # pyright: ignore[reportArgumentType]
    builder.add_node("aggregator", make_aggregator_node(corroboration_agent=corroboration_agent))  # pyright: ignore[reportArgumentType]
    builder.add_node("no_action_terminal", _no_action_terminal)
    builder.add_node("questions", make_questions_node(claim_questions_agent=claim_questions_agent))  # pyright: ignore[reportArgumentType]
    builder.add_node("retrieval", make_retrieval_node(answer_synthesis_agent=answer_synthesis_agent, deterministic_tools=deterministic_tools))  # pyright: ignore[reportArgumentType]
    builder.add_node("analysis", make_analysis_node(config=config, thesis_agent=thesis_agent))  # pyright: ignore[reportArgumentType]
    builder.add_node("validator", make_validator_node(behavioral_match_agent=behavioral_match_agent))  # pyright: ignore[reportArgumentType]
    builder.add_node("execution", make_execution_node(place_order=place_order))  # pyright: ignore[reportArgumentType]
    builder.add_node("notification", make_notification_node(send_email=send_email))  # pyright: ignore[reportArgumentType]

    builder.add_edge(START, "snapshot")
    builder.add_edge("snapshot", "aggregator")
    builder.add_conditional_edges("aggregator", signal_gate, {PROCEED: "questions", NO_ACTION: "no_action_terminal"})
    builder.add_edge("no_action_terminal", END)
    builder.add_edge("questions", "retrieval")
    builder.add_edge("retrieval", "analysis")
    builder.add_conditional_edges("analysis", terminal_state_router, {HALT: "notification", VALIDATE: "validator"})
    builder.add_conditional_edges("validator", determination_gate, {EXECUTE: "execution", NOTIFY: "notification"})
    builder.add_edge("execution", END)
    builder.add_edge("notification", END)

    return builder.compile()
