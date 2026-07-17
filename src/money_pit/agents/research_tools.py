"""Module containing the protocol types that decouple agent code from transport implementations in the money_pit package.

These protocols converge with `mcp.clients.ResearchDeps`: the MCP-backed
implementation satisfies both protocols structurally.
"""

from typing import Protocol
from typing import runtime_checkable

from money_pit.schemas.fetch_result import FetchResult


@runtime_checkable
class DeterministicResearchTools(Protocol):
    """Read-only structured fetches where the query is fully determined by question metadata."""

    def fetch_fred_series(self, series_id: str) -> FetchResult: ...

    def fetch_ticker_price(self, ticker: str) -> FetchResult: ...


@runtime_checkable
class OpenEndedResearchTools(Protocol):
    """Free-text retrieval interface used by the answer_synthesis LLM agent."""

    def brave_search(self, query: str, *, n_results: int = 5) -> list[str]: ...

    def edgar_search(self, query: str, *, n_results: int = 5) -> list[str]: ...
