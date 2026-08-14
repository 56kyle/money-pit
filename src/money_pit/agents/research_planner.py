"""Module building the tool-free A3 research-planning agent."""

from pydantic_ai import Agent
from pydantic_ai import NativeOutput

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import invoke_native_output
from money_pit.agents.models import openai_responses_model
from money_pit.agents.models import openai_responses_settings
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
    core: Agent[None, ResearchRoundPlan] = Agent(
        model=openai_responses_model(credentials, model_name=model_name),
        instructions=prompt,
        output_type=NativeOutput(ResearchRoundPlan, strict=True),
        model_settings=openai_responses_settings(
            stage="A3",
            model_name=model_name,
            instructions=prompt,
            output_type=ResearchRoundPlan,
        ),
        retries=1,
    )
    return BoundedInferenceAgent[ResearchPlanningRequest, ResearchRoundPlan].create(
        invoke=lambda message: invoke_native_output(core, message),
        system_prompt=prompt,
        output_type=ResearchRoundPlan,
        model_name=model_name,
        limits=budget,
        stage="A3",
        purpose="plan_research_wave",
        tracking=tracking,
    )
