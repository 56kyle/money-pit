"""Module building the capability-free A2 thesis-discovery agent."""

from pydantic_ai import Agent

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.config import Config
from money_pit.constants import OPENAI_MODEL_PREFIX
from money_pit.contracts import DiscoveryAgent
from money_pit.contracts import DiscoveryDraft
from money_pit.contracts import DiscoveryRequest
from money_pit.prompt_loader import system_prompt


def make_discovery_agent(
    config: Config,
    *,
    model: str | None = None,
    budget: InferenceBudgetLimits | None = None,
) -> DiscoveryAgent:
    """Return the typed A2 agent; deterministic code owns universe and weight authority."""
    model_name = f"{OPENAI_MODEL_PREFIX}{model or config.llm_model}"
    prompt = system_prompt("discovery")
    core: Agent[None, str] = Agent(model=model_name, output_type=str)
    return BoundedInferenceAgent[DiscoveryRequest, DiscoveryDraft].create(
        invoke=lambda message: DiscoveryDraft.model_validate_json(core.run_sync(message).output),
        system_prompt=prompt,
        output_type=DiscoveryDraft,
        model_name=model_name,
        limits=budget,
    )
