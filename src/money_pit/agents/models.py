"""Module containing explicit model-provider construction for inference agents."""

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from money_pit.config import Config
from money_pit.config import CredentialResolutionError


def openai_chat_model(config: Config, *, model_name: str | None = None) -> OpenAIChatModel:
    """Build an OpenAI model from the namespaced credential or fail explicitly."""
    credential = config.openai_api_key
    if credential is None or not credential.get_secret_value().strip():
        raise CredentialResolutionError("MONEY_PIT__OPENAI_API_KEY is required for model inference.")
    resolved_model = (model_name or config.llm_model).strip()
    if not resolved_model:
        raise CredentialResolutionError("MONEY_PIT__LLM_MODEL cannot be blank.")
    return OpenAIChatModel(
        resolved_model,
        provider=OpenAIProvider(api_key=credential.get_secret_value()),
    )
