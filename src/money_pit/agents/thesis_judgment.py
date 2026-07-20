"""Module containing the A4 LLM core producing claim disposition, thesis narratives, scenario estimates, and invalidation conditions as AnalysisJudgment for the money_pit package."""

from pydantic_ai import Agent

from money_pit.config import Config
from money_pit.constants import OPENAI_MODEL_PREFIX
from money_pit.contracts import ThesisAgent
from money_pit.prompt_loader import system_prompt
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.signals import AggregatedSignals


_PROMPT_NAME: str = "agent_4"


def make_thesis_judgment_agent(
    config: Config,
    *,
    model: str | None = None,
) -> ThesisAgent:
    """Return a callable that runs the A4 thesis judgment agent against the three pipeline inputs."""
    resolved_model: str = f"{OPENAI_MODEL_PREFIX}{model or config.llm_model}"
    agent: Agent[None, AnalysisJudgment] = Agent(
        model=resolved_model,
        output_type=AnalysisJudgment,
        system_prompt=system_prompt(_PROMPT_NAME),
        defer_model_check=False,
    )

    def run(
        signals: AggregatedSignals,
        portfolio: PortfolioSnapshot,
        answers: InitialAnswers,
    ) -> AnalysisJudgment:
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
        output: AnalysisJudgment = result.output
        return output

    return run
