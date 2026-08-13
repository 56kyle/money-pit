"""Module building the capability-free adversarial A4 synthesis agent."""

from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai import AgentRunResult

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import InferenceUsage
from money_pit.agents.inference import ProviderInferenceError
from money_pit.agents.models import openai_chat_model
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
    core: Agent[None, str] = Agent(model=openai_chat_model(credentials, model_name=model_name), output_type=str)
    return BoundedInferenceAgent[SynthesisRequest, SynthesisDraft].create(
        invoke=lambda message: _validated_synthesis(core.run_sync(message)),
        system_prompt=prompt,
        output_type=SynthesisDraft,
        model_name=model_name,
        limits=budget,
        stage="A4",
        purpose="synthesize_theses",
        tracking=tracking,
    )


def _validated_synthesis(result: AgentRunResult[str]) -> tuple[SynthesisDraft, InferenceUsage]:
    """Validate a provider result while preserving its reported usage."""
    usage = InferenceUsage.from_pydantic_ai(result.usage)
    try:
        return SynthesisDraft.model_validate_json(result.output), usage
    except ValidationError as error:
        raise ProviderInferenceError(usage=usage, failure_kind=type(error).__name__) from None
