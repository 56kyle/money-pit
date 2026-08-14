"""Module building the capability-free adversarial A4 synthesis agent."""

from pydantic_ai import Agent
from pydantic_ai import NativeOutput

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import invoke_native_output
from money_pit.agents.models import openai_responses_model
from money_pit.agents.models import openai_responses_settings
from money_pit.contracts import SynthesisAgent
from money_pit.contracts import SynthesisDraft
from money_pit.contracts import SynthesisRequest
from money_pit.prompt_loader import system_prompt
from money_pit.secrets import OpenAICredentials


def make_synthesis_agent(
    credentials: OpenAICredentials,
    *,
    model: str,
    budget: InferenceBudgetLimits | None = None,
    tracking: InferenceTracking | None = None,
) -> SynthesisAgent:
    """Return typed A4 synthesis with no source, search, portfolio-write, or broker tools."""
    model_name = model
    prompt = system_prompt("synthesis")
    core: Agent[None, SynthesisDraft] = Agent(
        model=openai_responses_model(credentials, model_name=model_name),
        instructions=prompt,
        output_type=NativeOutput(SynthesisDraft, strict=True),
        model_settings=openai_responses_settings(
            stage="A4",
            model_name=model_name,
            instructions=prompt,
            output_type=SynthesisDraft,
        ),
        retries=1,
    )
    return BoundedInferenceAgent[SynthesisRequest, SynthesisDraft].create(
        invoke=lambda message: invoke_native_output(core, message),
        system_prompt=prompt,
        output_type=SynthesisDraft,
        model_name=model_name,
        limits=budget,
        stage="A4",
        purpose="synthesize_theses",
        tracking=tracking,
    )
