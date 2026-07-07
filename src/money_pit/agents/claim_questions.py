"""A2 LLM core: claim-specific thesis-validation + invalidation questions only."""

import json
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.agent import AbstractAgent

from money_pit.config import Config
from money_pit.constants import ANTHROPIC_MODEL_PREFIX
from money_pit.contracts import ClaimQuestionsAgent
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.signals import Claim


_PROMPT_PATH: Path = Path(__file__).parent.parent.parent.parent / "data" / "agents" / "agent_2.md"
_SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")


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
        system_prompt=_SYSTEM_PROMPT,
    )

    def run(claims: list[Claim]) -> list[DraftQuestion]:
        user_message: str = "## Claims requiring thesis validation and invalidation analysis\n" + json.dumps(
            [c.model_dump(mode="json") for c in claims], indent=2
        )
        result = agent.run_sync(user_message)
        output: list[DraftQuestion] = result.output
        return output

    return run
