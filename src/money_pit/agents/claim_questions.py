"""A2 LLM core: claim-specific thesis-validation + invalidation questions only."""
import json
from pathlib import Path
from typing import Callable

from pydantic_ai import Agent

from money_pit.config import Config
from money_pit.schemas.questions import Question
from money_pit.schemas.signals import Claim

_PROMPT_PATH: Path = Path(__file__).parent.parent.parent.parent / "data" / "agents" / "agent_2.md"
_SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")


def make_claim_questions_agent(
    config: Config,
    *,
    model: str | None = None,
) -> Callable[[list[Claim]], list[Question]]:
    """Return a callable that generates thesis_validation and invalidation_conditions questions."""
    resolved_model: str = f"anthropic:{model or config.llm_model}"

    agent: Agent[None, list[Question]] = Agent(
        model=resolved_model,
        output_type=list[Question],
        system_prompt=_SYSTEM_PROMPT,
    )

    def run(claims: list[Claim]) -> list[Question]:
        user_message: str = (
            "## Claims requiring thesis validation and invalidation analysis\n"
            + json.dumps([c.model_dump(mode="json") for c in claims], indent=2)
        )
        result = agent.run_sync(user_message)
        output: list[Question] = result.output
        return output

    return run
