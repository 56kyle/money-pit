import json
from typing import ClassVar

import pytest
from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetExceededError
from money_pit.agents.budget import InferenceBudgetLimits


class _Request(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    evidence: str


class _Response(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    accepted: bool


def _agent(
    maximum_characters: int,
    calls: list[str],
) -> BoundedInferenceAgent[_Request, _Response]:
    return BoundedInferenceAgent[_Request, _Response].create(
        invoke=lambda message: calls.append(message) or _Response(accepted=True),
        system_prompt="system prompt",
        output_type=_Response,
        model_name="provider:model",
        limits=InferenceBudgetLimits(
            maximum_characters=maximum_characters,
            response_reserve_characters=17,
        ),
    )


def test_bounded_inference_agent_counts_the_complete_rendered_envelope() -> None:
    calls: list[str] = []
    generous = _agent(10_000, calls)
    request = _Request(evidence="material evidence")
    exact_limit = (
        10_000
        - generous.request_character_allowance
        + len(
            request.model_dump_json(indent=2),
        )
    )
    exact = _agent(exact_limit, calls)

    result = exact(request)

    assert result == _Response(accepted=True)
    assert calls == [
        "\n".join(
            (
                "SYSTEM",
                "system prompt",
                "OUTPUT_SCHEMA",
                json.dumps(_Response.model_json_schema(), sort_keys=True, separators=(",", ":"), allow_nan=False),
                "METADATA",
                '{"model":"provider:model"}',
                "USER",
                request.model_dump_json(indent=2),
            )
        )
    ]


def test_bounded_inference_agent_rejects_before_provider_io_when_response_reserve_would_overflow() -> None:
    calls: list[str] = []
    generous = _agent(10_000, calls)
    request = _Request(evidence="material evidence")
    exact_limit = (
        10_000
        - generous.request_character_allowance
        + len(
            request.model_dump_json(indent=2),
        )
    )
    undersized = _agent(exact_limit - 1, calls)

    with pytest.raises(InferenceBudgetExceededError) as raised:
        _ = undersized(request)

    breakdown = raised.value.breakdown
    assert breakdown.rendered_total == exact_limit
    assert breakdown.response_reserve_characters == 17
    assert breakdown.system_prompt_characters == len("system prompt")
    assert breakdown.user_message_characters == len(request.model_dump_json(indent=2))
    assert breakdown.serialized_request_characters + 17 == exact_limit
    assert calls == []
