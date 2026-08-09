"""Module building the tool-free A3 research-planning agent."""

from pydantic_ai import Agent

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.config import Config
from money_pit.constants import OPENAI_MODEL_PREFIX
from money_pit.contracts import ResearchPlanningAgent
from money_pit.contracts import ResearchPlanningRequest
from money_pit.contracts import ResearchRoundPlan
from money_pit.prompt_loader import system_prompt


def make_research_planning_agent(
    config: Config,
    *,
    model: str | None = None,
    budget: InferenceBudgetLimits | None = None,
) -> ResearchPlanningAgent:
    """Return A3 planning; the harness validates and executes every provider request."""
    model_name = f"{OPENAI_MODEL_PREFIX}{model or config.llm_model}"
    prompt = system_prompt("research_planning")
    core: Agent[None, str] = Agent(model=model_name, output_type=str)
    return BoundedInferenceAgent[ResearchPlanningRequest, ResearchRoundPlan].create(
        invoke=lambda message: ResearchRoundPlan.model_validate_json(core.run_sync(message).output),
        system_prompt=prompt,
        output_type=ResearchRoundPlan,
        model_name=model_name,
        limits=budget,
    )
