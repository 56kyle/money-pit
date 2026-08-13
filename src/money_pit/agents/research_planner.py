"""Module building the tool-free A3 research-planning agent."""

from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai import AgentRunResult

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import InferenceUsage
from money_pit.agents.inference import ProviderInferenceError
from money_pit.agents.models import openai_chat_model
from money_pit.contracts import ResearchPlanningAgent
from money_pit.contracts import ResearchPlanningRequest
from money_pit.contracts import ResearchRoundPlan
from money_pit.prompt_loader import system_prompt
from money_pit.secrets import OpenAICredentials


def make_research_planning_agent(
    credentials: OpenAICredentials,
    *,
    model: str,
    budget: InferenceBudgetLimits | None = None,
    tracking: InferenceTracking | None = None,
) -> ResearchPlanningAgent:
    """Return A3 planning; the harness validates and executes every provider request."""
    model_name = model
    prompt = system_prompt("research_planning")
    core: Agent[None, str] = Agent(model=openai_chat_model(credentials, model_name=model_name), output_type=str)
    return BoundedInferenceAgent[ResearchPlanningRequest, ResearchRoundPlan].create(
        invoke=lambda message: _validated_research_plan(core.run_sync(message)),
        system_prompt=prompt,
        output_type=ResearchRoundPlan,
        model_name=model_name,
        limits=budget,
        stage="A3",
        purpose="plan_research_wave",
        tracking=tracking,
    )


def _validated_research_plan(result: AgentRunResult[str]) -> tuple[ResearchRoundPlan, InferenceUsage]:
    """Validate a provider result while preserving its reported usage."""
    usage = InferenceUsage.from_pydantic_ai(result.usage)
    try:
        return ResearchRoundPlan.model_validate_json(result.output), usage
    except ValidationError as error:
        raise ProviderInferenceError(usage=usage, failure_kind=type(error).__name__) from None
