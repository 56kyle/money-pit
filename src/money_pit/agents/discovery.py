"""Module building the capability-free A2 thesis-discovery agent."""

from pydantic_ai import Agent
from pydantic_ai import NativeOutput

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import invoke_native_output
from money_pit.agents.models import openai_responses_model
from money_pit.agents.models import openai_responses_settings
from money_pit.contracts import DiscoveryAgent
from money_pit.contracts import DiscoveryDraft
from money_pit.contracts import DiscoveryRequest
from money_pit.prompt_loader import system_prompt
from money_pit.secrets import OpenAICredentials


def make_discovery_agent(
    credentials: OpenAICredentials,
    *,
    model: str,
    budget: InferenceBudgetLimits | None = None,
    tracking: InferenceTracking | None = None,
) -> DiscoveryAgent:
    """Return the typed A2 agent; deterministic code owns universe and weight authority."""
    model_name = model
    prompt = system_prompt("discovery")
    core: Agent[None, DiscoveryDraft] = Agent(
        model=openai_responses_model(credentials, model_name=model_name),
        instructions=prompt,
        output_type=NativeOutput(DiscoveryDraft, strict=True),
        model_settings=openai_responses_settings(
            stage="A2",
            model_name=model_name,
            instructions=prompt,
            output_type=DiscoveryDraft,
        ),
        retries=1,
    )
    return BoundedInferenceAgent[DiscoveryRequest, DiscoveryDraft].create(
        invoke=lambda message: invoke_native_output(core, message),
        system_prompt=prompt,
        output_type=DiscoveryDraft,
        model_name=model_name,
        limits=budget,
        stage="A2",
        purpose="discover_candidates",
        tracking=tracking,
    )
