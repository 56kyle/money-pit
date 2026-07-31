"""Module containing the StateGraph assembly (add_node / add_edge / add_conditional_edges) for the money_pit package."""

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver  # pyright: ignore[reportMissingTypeStubs]
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
from money_pit.contracts import FillObserver
from money_pit.contracts import OrderPlacer
from money_pit.contracts import PortfolioFetcher
from money_pit.contracts import ResolveInstrumentFacts
from money_pit.contracts import ThesisAgent
from money_pit.contracts import ToolManifest
from money_pit.graph.edges import EXECUTE
from money_pit.graph.edges import FINALIZE
from money_pit.graph.edges import HALT
from money_pit.graph.edges import NO_ACTION
from money_pit.graph.edges import NOTIFY
from money_pit.graph.edges import PROCEED
from money_pit.graph.edges import TERMINATE
from money_pit.graph.edges import VALIDATE
from money_pit.graph.edges import determination_router
from money_pit.graph.edges import execution_outcome_router
from money_pit.graph.edges import post_notification_router
from money_pit.graph.edges import recovery_router
from money_pit.graph.edges import signal_gate
from money_pit.graph.edges import terminal_state_router
from money_pit.graph.state import PipelineState
from money_pit.pipeline.aggregator import make_aggregator_node
from money_pit.pipeline.analysis import make_analysis_node
from money_pit.pipeline.chain import EXECUTION_NODE
from money_pit.pipeline.chain import RECOVERY_NODE
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.chain import interrupt_before_successor_of
from money_pit.pipeline.determination import make_determination_node
from money_pit.pipeline.determination import make_finalizer_node
from money_pit.pipeline.execution import make_execution_node
from money_pit.pipeline.notification import make_notification_node
from money_pit.pipeline.questions import make_questions_node
from money_pit.pipeline.recovery import make_recovery_node
from money_pit.pipeline.retrieval import make_retrieval_node
from money_pit.pipeline.snapshot import make_snapshot_node
from money_pit.pipeline.validator import make_validator_node
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.execution_policy import ExecutionMode


_THREAD_ID_CONFIG_KEY: str = "thread_id"


def thread_config(slug: str) -> RunnableConfig:
    """Address the checkpointed thread for one pipeline run by its slug."""
    return RunnableConfig(configurable={_THREAD_ID_CONFIG_KEY: slug})


def _no_action_terminal(state: PipelineState) -> PipelineState:
    result: PipelineState = {
        "terminal_state": TerminalState.NO_ACTION,
        "completed_steps": [*list(state.get("completed_steps") or []), "no_action_terminal"],
    }
    return result


def _authority_aware_determination_router(
    state: PipelineState,
    execution_mode: ExecutionMode,
    *,
    legacy_injected_write_authority: bool,
) -> str:
    """Route legacy runs away from capital writes unless autonomy is explicit."""
    route: str = determination_router(state)
    if route != EXECUTE:
        return route
    if execution_mode is ExecutionMode.AUTONOMOUS:
        return FINALIZE
    if execution_mode is ExecutionMode.APPROVAL_REQUIRED and legacy_injected_write_authority:
        return EXECUTE
    if execution_mode in (ExecutionMode.OBSERVE, ExecutionMode.APPROVAL_REQUIRED):
        return FINALIZE
    raise AssertionError(f"Unhandled execution mode: {execution_mode!r}")


def build_graph(
    *,
    fetch_portfolio: PortfolioFetcher,
    corroboration_agent: CorroborationAgent,
    claim_questions_agent: ClaimQuestionsAgent,
    answer_synthesis_agent: AnswerSynthesisAgent,
    deterministic_tools: DeterministicResearchTools,
    config: Config,
    thesis_agent: ThesisAgent,
    resolve_instrument_facts: ResolveInstrumentFacts,
    place_order: OrderPlacer | None,
    observe_fill: FillObserver,
    send_email: EmailSender,
    manifest: ToolManifest | None = None,
    through: Stage | None = None,
    legacy_injected_write_authority: bool | None = None,
) -> CompiledStateGraph[PipelineState]:
    """Assemble and compile the full money-pit LangGraph pipeline, pausing after `through` when one is named and raising PlanningChainError when `through` has no successor (see ADR 0035 and ADR 0038)."""
    builder: StateGraph[PipelineState] = StateGraph(state_schema=PipelineState)
    resolved_legacy_authority: bool = (
        place_order is not None if legacy_injected_write_authority is None else legacy_injected_write_authority
    )

    builder.add_node(RECOVERY_NODE, make_recovery_node(observe_fill=observe_fill, send_email=send_email))
    builder.add_node("snapshot", make_snapshot_node(fetch_portfolio=fetch_portfolio))
    builder.add_node("aggregator", make_aggregator_node(corroboration_agent=corroboration_agent))
    builder.add_node("no_action_terminal", _no_action_terminal)
    builder.add_node(
        "questions",
        make_questions_node(
            claim_questions_agent=claim_questions_agent,
            current_events_lookback_days=config.current_events_lookback_days,
        ),
    )
    builder.add_node(
        "retrieval",
        make_retrieval_node(answer_synthesis_agent=answer_synthesis_agent, deterministic_tools=deterministic_tools),
    )
    builder.add_node(
        "analysis",
        make_analysis_node(config=config, thesis_agent=thesis_agent, resolve_instrument_facts=resolve_instrument_facts),
    )
    builder.add_node("validator", make_validator_node(manifest=manifest))
    builder.add_node("determination", make_determination_node())
    builder.add_node(
        EXECUTION_NODE,
        make_execution_node(
            place_order=place_order,
            observe_fill=observe_fill,
            poll_interval=config.execution_fill_poll_interval_seconds,
            poll_timeout=config.execution_fill_poll_timeout_seconds,
        ),
    )
    builder.add_node("notification", make_notification_node(send_email=send_email))
    builder.add_node("finalizer", make_finalizer_node())

    builder.add_edge(START, RECOVERY_NODE)
    builder.add_conditional_edges(RECOVERY_NODE, recovery_router, {PROCEED: "snapshot", HALT: END})
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
        lambda state: _authority_aware_determination_router(
            state,
            config.execution_mode,
            legacy_injected_write_authority=resolved_legacy_authority,
        ),
        {EXECUTE: EXECUTION_NODE, NOTIFY: "notification", FINALIZE: "finalizer"},
    )
    builder.add_conditional_edges(
        EXECUTION_NODE,
        execution_outcome_router,
        {NOTIFY: "notification", FINALIZE: "finalizer"},
    )
    builder.add_conditional_edges(
        "notification",
        post_notification_router,
        {TERMINATE: END, FINALIZE: "finalizer"},
    )
    builder.add_edge("finalizer", END)

    interrupt_before: list[str] = interrupt_before_successor_of(through)
    if interrupt_before:
        return builder.compile(checkpointer=InMemorySaver(), interrupt_before=interrupt_before)
    return builder.compile()
