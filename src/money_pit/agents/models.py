"""Module containing explicit model-provider construction for inference agents."""

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from money_pit.secrets import OpenAICredentials


def openai_chat_model(credentials: OpenAICredentials, *, model_name: str) -> OpenAIChatModel:
    """Build an OpenAI model from explicit capability credentials."""
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(api_key=credentials.api_key.get_secret_value()),
    )


def openai_responses_model(credentials: OpenAICredentials, *, model_name: str) -> OpenAIResponsesModel:
    """Build an OpenAI Responses model from explicit capability credentials."""
    return OpenAIResponsesModel(
        model_name,
        provider=OpenAIProvider(api_key=credentials.api_key.get_secret_value()),
    )
