"""Module building the capability-free adversarial A4 synthesis agent."""

from pydantic_ai import Agent

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.models import openai_chat_model
from money_pit.config import Config
from money_pit.contracts import SynthesisAgent
from money_pit.contracts import SynthesisDraft
from money_pit.contracts import SynthesisRequest
from money_pit.prompt_loader import system_prompt


def make_synthesis_agent(
    config: Config,
    *,
    model: str | None = None,
    budget: InferenceBudgetLimits | None = None,
) -> SynthesisAgent:
    """Return typed A4 synthesis with no source, search, portfolio-write, or broker tools."""
    model_name = model or config.llm_model
    prompt = system_prompt("synthesis")
    core: Agent[None, str] = Agent(model=openai_chat_model(config, model_name=model), output_type=str)
    return BoundedInferenceAgent[SynthesisRequest, SynthesisDraft].create(
        invoke=lambda message: SynthesisDraft.model_validate_json(core.run_sync(message).output),
        system_prompt=prompt,
        output_type=SynthesisDraft,
        model_name=model_name,
        limits=budget,
    )
