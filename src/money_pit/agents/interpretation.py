"""Module building the capability-free A1 interpretation agent."""

from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai import AgentRunResult

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import InferenceUsage
from money_pit.agents.inference import ProviderInferenceError
from money_pit.agents.models import openai_chat_model
from money_pit.contracts import InterpretationAgent
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import InterpretationRequest
from money_pit.prompt_loader import system_prompt
from money_pit.secrets import OpenAICredentials


def _invoke_interpretation_with_one_validation_retry(
    core: Agent[None, str],
    message: str,
) -> tuple[InterpretationDraft, InferenceUsage]:
    """Validate one model response, retrying one malformed typed response."""
    first_result: AgentRunResult[str] = core.run_sync(message)
    first_usage = InferenceUsage.from_pydantic_ai(first_result.usage)
    try:
        return InterpretationDraft.model_validate_json(first_result.output), first_usage
    except ValidationError:
        try:
            retry_result: AgentRunResult[str] = core.run_sync(message)
        except Exception as error:  # noqa: BLE001 - preserve first-call usage on provider failure
            raise ProviderInferenceError(
                usage=first_usage,
                failure_kind=type(error).__name__,
            ) from None
        combined_usage = first_usage + InferenceUsage.from_pydantic_ai(retry_result.usage)
        try:
            return InterpretationDraft.model_validate_json(retry_result.output), combined_usage
        except ValidationError as error:
            raise ProviderInferenceError(
                usage=combined_usage,
                failure_kind=type(error).__name__,
            ) from None


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
    core: Agent[None, str] = Agent(model=openai_chat_model(credentials, model_name=model_name), output_type=str)
    return BoundedInferenceAgent[InterpretationRequest, InterpretationDraft].create(
        invoke=lambda message: _invoke_interpretation_with_one_validation_retry(core, message),
        system_prompt=prompt,
        output_type=InterpretationDraft,
        model_name=model_name,
        limits=budget,
        stage="A1",
        purpose="interpret_evidence",
        tracking=tracking,
    )
