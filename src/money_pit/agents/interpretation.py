"""Module building the capability-free A1 interpretation agent."""

from pydantic_ai import Agent
from pydantic_ai import NativeOutput

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import invoke_native_output
from money_pit.agents.models import openai_responses_model
from money_pit.agents.models import openai_responses_settings
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
    tracking: InferenceTracking | None = None,
) -> InterpretationAgent:
    """Return the typed A1 agent with no research, portfolio, or broker tools."""
    model_name = model
    prompt = system_prompt("interpretation")
    core: Agent[None, InterpretationDraft] = Agent(
        model=openai_responses_model(credentials, model_name=model_name),
        instructions=prompt,
        output_type=NativeOutput(InterpretationDraft, strict=True),
        model_settings=openai_responses_settings(
            stage="A1",
            model_name=model_name,
            instructions=prompt,
            output_type=InterpretationDraft,
        ),
        retries=1,
    )
    return BoundedInferenceAgent[InterpretationRequest, InterpretationDraft].create(
        invoke=lambda message: invoke_native_output(core, message),
        system_prompt=prompt,
        output_type=InterpretationDraft,
        model_name=model_name,
        limits=budget,
        stage="A1",
        purpose="interpret_evidence",
        tracking=tracking,
    )
