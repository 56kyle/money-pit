"""Module building the capability-free A2 thesis-discovery agent."""

from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai import AgentRunResult

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import InferenceUsage
from money_pit.agents.inference import ProviderInferenceError
from money_pit.agents.models import openai_chat_model
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
    core: Agent[None, str] = Agent(model=openai_chat_model(credentials, model_name=model_name), output_type=str)
    return BoundedInferenceAgent[DiscoveryRequest, DiscoveryDraft].create(
        invoke=lambda message: _validated_discovery(core.run_sync(message)),
        system_prompt=prompt,
        output_type=DiscoveryDraft,
        model_name=model_name,
        limits=budget,
        stage="A2",
        purpose="discover_candidates",
        tracking=tracking,
    )


def _validated_discovery(result: AgentRunResult[str]) -> tuple[DiscoveryDraft, InferenceUsage]:
    """Validate a provider result while preserving its reported usage."""
    usage = InferenceUsage.from_pydantic_ai(result.usage)
    try:
        return DiscoveryDraft.model_validate_json(result.output), usage
    except ValidationError as error:
        raise ProviderInferenceError(usage=usage, failure_kind=type(error).__name__) from None
