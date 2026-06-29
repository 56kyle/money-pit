"""A3 LLM core: open-ended Brave/EDGAR lookups + answer synthesis."""
import json
from pathlib import Path
from typing import Callable

from pydantic_ai import Agent, RunContext

from money_pit.agents.research_tools import OpenEndedResearchTools
from money_pit.config import Config
from money_pit.schemas.answers import Answer
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.questions import Question

_PROMPT_PATH: Path = Path(__file__).parent.parent.parent.parent / "data" / "agents" / "agent_3.md"
_SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")

_OPEN_ENDED_CATEGORIES: frozenset[QuestionCategory] = frozenset({
    QuestionCategory.THESIS_VALIDATION,
    QuestionCategory.CURRENT_EVENTS,
    QuestionCategory.INVALIDATION_CONDITIONS,
})


def make_answer_synthesis_agent(
    tools: OpenEndedResearchTools,
    config: Config,
    *,
    model: str | None = None,
) -> Callable[[list[Question], list[SourceRef]], list[Answer]]:
    """Build and return the A3 answer synthesis callable backed by a pydantic-ai Agent.

    The returned callable accepts only open-ended question categories and raises on
    agent error; the retrieval pipeline node is responsible for wrapping in try/except.
    """
    resolved_model: str = f"anthropic:{model or config.llm_model}"

    agent: Agent[OpenEndedResearchTools, list[Answer]] = Agent(
        model=resolved_model,
        output_type=list[Answer],
        deps_type=OpenEndedResearchTools,
        system_prompt=_SYSTEM_PROMPT,
    )

    @agent.tool
    def brave_search(ctx: RunContext[OpenEndedResearchTools], query: str, n_results: int = 5) -> str:  # pyright: ignore[reportUnusedFunction]
        snippets = ctx.deps.brave_search(query, n_results=n_results)
        return "\n---\n".join(snippets) if snippets else "No results found."

    @agent.tool
    def edgar_search(ctx: RunContext[OpenEndedResearchTools], query: str, n_results: int = 5) -> str:  # pyright: ignore[reportUnusedFunction]
        excerpts = ctx.deps.edgar_search(query, n_results=n_results)
        return "\n---\n".join(excerpts) if excerpts else "No results found."

    def run(questions: list[Question], sources: list[SourceRef]) -> list[Answer]:
        open_ended_qs = [q for q in questions if q.category in _OPEN_ENDED_CATEGORIES]
        if not open_ended_qs:
            return []

        user_message = (
            "## Questions to answer\n"
            + json.dumps([q.model_dump(mode="json") for q in open_ended_qs], indent=2)
            + "\n\n## Source context\n"
            + json.dumps([s.model_dump(mode="json") for s in sources], indent=2)
        )

        result = agent.run_sync(user_message, deps=tools)
        return result.output

    return run
