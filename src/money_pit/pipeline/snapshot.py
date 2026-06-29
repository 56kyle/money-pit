"""Calls Alpaca read MCP, writes portfolio_snapshot.json."""
from pathlib import Path
from typing import Callable

from money_pit.graph.state import PipelineState
from money_pit.schemas.portfolio import PortfolioSnapshot


def make_snapshot_node(
    fetch_portfolio: Callable[[str], PortfolioSnapshot],
) -> Callable[[PipelineState], dict[str, object]]:
    """Return a LangGraph node that fetches and persists the portfolio snapshot."""

    def snapshot_node(state: PipelineState) -> dict[str, object]:
        """Fetch the portfolio snapshot for state["slug"] and write it to working_dir."""
        slug: str | None = state.get("slug")
        if slug is None:
            raise ValueError("PipelineState missing required key 'slug'")
        working_dir_str: str | None = state.get("working_dir")
        if working_dir_str is None:
            raise ValueError("PipelineState missing required key 'working_dir'")
        working_dir: Path = Path(working_dir_str)
        snapshot: PortfolioSnapshot = fetch_portfolio(slug)
        _ = (working_dir / "portfolio_snapshot.json").write_text(
            snapshot.model_dump_json(indent=2),
            encoding="utf-8",
        )
        prior_steps: list[str] = list(state.get("completed_steps") or [])
        result: dict[str, object] = {"completed_steps": prior_steps + ["snapshot"]}
        return result

    return snapshot_node
