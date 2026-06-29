"""A4 LLM core: claim disposition, thesis narratives, scenario estimates, invalidation conditions → AnalysisJudgment."""
from collections.abc import Callable
from pathlib import Path

from pydantic_ai import Agent

from money_pit.config import Config
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.signals import AggregatedSignals


_PROMPT_PATH: Path = Path(__file__).parent.parent.parent.parent / "data" / "agents" / "agent_4.md"
_SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")


def make_thesis_judgment_agent(
    config: Config,
    *,
    model: str | None = None,
) -> Callable[[AggregatedSignals, PortfolioSnapshot, InitialAnswers], list[AnalysisJudgment]]:
    """Return a callable that runs the A4 thesis judgment agent against the three pipeline inputs."""
    resolved_model: str = f"anthropic:{model or config.llm_model}"
    agent: Agent[None, list[AnalysisJudgment]] = Agent(
        model=resolved_model,
        output_type=list[AnalysisJudgment],
        system_prompt=_SYSTEM_PROMPT,
        defer_model_check=False,
    )

    def run(
        signals: AggregatedSignals,
        portfolio: PortfolioSnapshot,
        answers: InitialAnswers,
    ) -> list[AnalysisJudgment]:
        """Execute the A4 judgment agent against the three pipeline inputs."""
        user_message: str = (
            "## Aggregated Signals\n"
            + signals.model_dump_json(indent=2)
            + "\n\n## Portfolio Snapshot\n"
            + portfolio.model_dump_json(indent=2)
            + "\n\n## Research Answers\n"
            + answers.model_dump_json(indent=2)
        )
        result = agent.run_sync(user_message)
        output: list[AnalysisJudgment] = result.output
        return output

    return run
