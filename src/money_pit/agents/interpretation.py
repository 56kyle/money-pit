"""Module building the capability-free A1 interpretation agent."""

from pydantic_ai import Agent

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.models import openai_chat_model
from money_pit.contracts import InterpretationAgent
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import InterpretationRequest
from money_pit.prompt_loader import system_prompt
from money_pit.secrets import OpenAICredentials


def make_interpretation_agent(
    credentials: OpenAICredentials,
    *,
    model: str,
    budget: InferenceBudgetLimits | None = None,
) -> InterpretationAgent:
    """Return the typed A1 agent with no research, portfolio, or broker tools."""
    model_name = model
    prompt = system_prompt("interpretation")
    core: Agent[None, str] = Agent(model=openai_chat_model(credentials, model_name=model_name), output_type=str)
    return BoundedInferenceAgent[InterpretationRequest, InterpretationDraft].create(
        invoke=lambda message: InterpretationDraft.model_validate_json(core.run_sync(message).output),
        system_prompt=prompt,
        output_type=InterpretationDraft,
        model_name=model_name,
        limits=budget,
    )
