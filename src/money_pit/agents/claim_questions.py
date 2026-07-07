"""Module containing the A2 LLM core for claim-specific thesis-validation and invalidation questions in the money_pit package."""

import json

from pydantic_ai import Agent
from pydantic_ai.agent import AbstractAgent

from money_pit.prompt_loader import system_prompt
from money_pit.config import Config
from money_pit.constants import ANTHROPIC_MODEL_PREFIX
from money_pit.contracts import ClaimQuestionsAgent
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.signals import Claim


_PROMPT_NAME: str = "agent_2"


def make_claim_questions_agent(
    config: Config,
    *,
    model: str | None = None,
) -> ClaimQuestionsAgent:
    """Return a callable that generates thesis_validation and invalidation_conditions questions."""
    resolved_model: str = f"{ANTHROPIC_MODEL_PREFIX}{model or config.llm_model}"

    agent: AbstractAgent[object, list[DraftQuestion]] = Agent(
        model=resolved_model,
        output_type=list[DraftQuestion],
        system_prompt=system_prompt(_PROMPT_NAME),
    )

    def run(claims: list[Claim]) -> list[DraftQuestion]:
        user_message: str = "## Claims requiring thesis validation and invalidation analysis\n" + json.dumps(
            [c.model_dump(mode="json") for c in claims], indent=2
        )
        result = agent.run_sync(user_message)
        output: list[DraftQuestion] = result.output
        return output

    return run
