"""Tests for money_pit.graph.graph — the assembly of the compiled pipeline graph and the `--through` bound it applies.

`PLANNING_CHAIN` writes its node names as independent literals rather than deriving them from the graph
(ADR 0038), so nothing but a test holds the two sides together. A chain entry naming a node `build_graph` never
registers raises out of LangGraph's `compile`, which is loud but lands in front of an operator only after an
ingest has been paid for. Compiling every `--through` bound here moves that failure into CI instead.

The eleven dependencies are stubs that raise when called: assembling a graph wires node factories and must not
invoke the agents, fetchers, or senders behind them, so a stub that raises pins that too. The tool manifest is
passed explicitly because the default resolves the committed order schema eagerly at build time, which would
tie this test to that file's state rather than to the graph's shape.
"""

from typing import NoReturn

import pytest
from langgraph.graph.state import CompiledStateGraph  # pyright: ignore[reportMissingTypeStubs]

from money_pit.config import Config
from money_pit.contracts import ToolManifest
from money_pit.graph.graph import build_graph
from money_pit.graph.state import PipelineState
from money_pit.pipeline.chain import PLANNING_CHAIN
from money_pit.pipeline.chain import Stage


_UNCALLED_DEPENDENCY_MESSAGE: str = "build_graph assembled the graph by invoking a dependency, which it must not do."

_EVERY_BOUND: list[Stage | None] = [None, *list(Stage)]


def _uncalled_dependency(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError(_UNCALLED_DEPENDENCY_MESSAGE)


class _UncalledResearchTools:
    """A DeterministicResearchTools stand-in whose fetches raise, since building a graph must not perform any."""

    def fetch_fred_series(self, series_id: str) -> NoReturn:
        return _uncalled_dependency(series_id)

    def fetch_ticker_price(self, ticker: str) -> NoReturn:
        return _uncalled_dependency(ticker)


@pytest.fixture
def graph_config() -> Config:
    """The credentials are inert strings: no node is run here, so nothing resolves them."""
    return Config(alpaca_service="the-service", alpaca_username="the-user", alpaca_paper=True)


@pytest.fixture
def graph_manifest() -> ToolManifest:
    """An empty manifest stands in for the pinned one, whose only role at build time is being resolvable."""
    return {}


@pytest.fixture
def compiled_graph(
    request: pytest.FixtureRequest, graph_config: Config, graph_manifest: ToolManifest
) -> CompiledStateGraph[PipelineState]:
    """Build the full graph under the `--through` bound named by the requesting test, defaulting to unbounded."""
    through: Stage | None = getattr(request, "param", None)
    return build_graph(
        fetch_portfolio=_uncalled_dependency,
        corroboration_agent=_uncalled_dependency,
        claim_questions_agent=_uncalled_dependency,
        answer_synthesis_agent=_uncalled_dependency,
        deterministic_tools=_UncalledResearchTools(),
        config=graph_config,
        thesis_agent=_uncalled_dependency,
        resolve_instrument_facts=_uncalled_dependency,
        place_order=_uncalled_dependency,
        observe_fill=_uncalled_dependency,
        send_email=_uncalled_dependency,
        manifest=graph_manifest,
        through=through,
    )


@pytest.mark.parametrize(
    "compiled_graph",
    [pytest.param(bound, id=bound.value if bound is not None else "unbounded") for bound in _EVERY_BOUND],
    indirect=True,
)
def test_build_graph_with_each_bound_compiles(compiled_graph: CompiledStateGraph[PipelineState]) -> None:
    """Every `--through` an operator can name must reach a node the graph actually registers.

    `compile(interrupt_before=[...])` rejects an unregistered name, so a chain that drifts away from the
    registered nodes fails here rather than at the operator's console after an ingest has been paid for.
    """
    assert compiled_graph.nodes


def test_build_graph_registers_every_planning_chain_node(compiled_graph: CompiledStateGraph[PipelineState]) -> None:
    """The correspondence ADR 0038 leaves to a test: each independently-written chain literal names a real node.

    Compiling the bounds only exercises the successors; `recovery` heads the chain and is never a successor, so
    it would otherwise go unwitnessed.
    """
    assert set(PLANNING_CHAIN) <= set(compiled_graph.nodes)
