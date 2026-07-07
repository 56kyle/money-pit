"""Module containing the node that calls the Alpaca read MCP and writes portfolio_snapshot.json for the money_pit package."""

from collections.abc import Callable
from pathlib import Path

from money_pit.constants import PORTFOLIO_SNAPSHOT_FILENAME
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.schemas.portfolio import PortfolioSnapshot


def make_snapshot_node(
    fetch_portfolio: Callable[[str], PortfolioSnapshot],
) -> PipelineNode:
    """Return a LangGraph node that fetches and persists the portfolio snapshot."""

    def snapshot_node(state: PipelineState) -> PipelineState:
        """Fetch the portfolio snapshot for state["slug"] and write it to working_dir."""
        slug: str = require_slug(state)
        working_dir: Path = require_working_dir(state)
        snapshot: PortfolioSnapshot = fetch_portfolio(slug)
        _ = (working_dir / PORTFOLIO_SNAPSHOT_FILENAME).write_text(
            snapshot.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return {"completed_steps": with_completed_step(state, "snapshot")}

    return snapshot_node
