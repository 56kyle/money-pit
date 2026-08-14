"""Module containing explicit model-provider construction for inference agents."""

import hashlib
import json

from pydantic import BaseModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from money_pit.agents.inference import InferenceStage
from money_pit.constants import APP_VERSION
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


def openai_responses_settings(
    *,
    stage: InferenceStage,
    model_name: str,
    instructions: str,
    output_type: type[BaseModel],
) -> OpenAIResponsesModelSettings:
    """Return stateless Responses settings with a stable prompt-prefix cache identity."""
    return OpenAIResponsesModelSettings(
        openai_store=False,
        openai_reasoning_effort="medium",
        openai_prompt_cache_key=_prompt_cache_key(
            stage=stage,
            model_name=model_name,
            instructions=instructions,
            output_type=output_type,
        ),
    )


def _prompt_cache_key(
    *,
    stage: InferenceStage,
    model_name: str,
    instructions: str,
    output_type: type[BaseModel],
) -> str:
    """Hash only stable provider-prefix inputs, excluding workflow and request identity."""
    cache_identity = json.dumps(
        {
            "instructions": instructions,
            "model": model_name,
            "output_schema": output_type.model_json_schema(),
            "prompt_version": APP_VERSION,
            "stage": stage,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()
