from typing import ClassVar

import pytest
from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.agents.budget import BoundedInferenceAgent
from money_pit.agents.budget import InferenceBudgetExceededError
from money_pit.agents.budget import InferenceBudgetLimits
from money_pit.agents.inference import InferenceCallRecord
from money_pit.agents.inference import InferenceInvocationError
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import InferenceUsage
from money_pit.agents.inference import ProviderInferenceError
from money_pit.agents.inference import deterministic_request_hash


class _Request(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    evidence: str


class _Response(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    accepted: bool


class _CallSink:
    def __init__(self) -> None:
        self.records: list[InferenceCallRecord] = []

    def record_inference_call(self, record: InferenceCallRecord) -> None:
        self.records.append(record)


def _agent(
    maximum_characters: int,
    calls: list[str],
) -> BoundedInferenceAgent[_Request, _Response]:
    return BoundedInferenceAgent[_Request, _Response].create(
        invoke=lambda message: (
            calls.append(message) or _Response(accepted=True),
            InferenceUsage(request_count=1),
        ),
        system_prompt="system prompt",
        output_type=_Response,
        model_name="provider:model",
        stage="A2",
        purpose="test_budget",
        limits=InferenceBudgetLimits(
            maximum_characters=maximum_characters,
            response_reserve_characters=17,
        ),
    )


def test_bounded_inference_agent_sends_only_compact_request_json() -> None:
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

    assert result.output == _Response(accepted=True)
    assert calls == ['{"evidence":"material evidence"}']


def test_bounded_inference_agent_hashes_the_complete_provider_visible_request() -> None:
    calls: list[str] = []
    agent = _agent(10_000, calls)

    result = agent(_Request(evidence="material evidence"))

    rendered_request = agent._system_prompt + agent._output_schema + calls[0]  # pyright: ignore[reportPrivateUsage]
    assert result.request_hash == deterministic_request_hash(rendered_request)


def test_bounded_inference_agent_rejects_before_provider_io_when_response_reserve_would_overflow() -> None:
    calls: list[str] = []
    generous = _agent(10_000, calls)
    request = _Request(evidence="material evidence")
    exact_limit = 10_000 - generous.request_character_allowance + len('{"evidence":"material evidence"}')
    undersized = _agent(exact_limit - 1, calls)

    with pytest.raises(InferenceBudgetExceededError) as raised:
        _ = undersized(request)

    breakdown = raised.value.breakdown
    assert breakdown.rendered_total == exact_limit
    assert breakdown.response_reserve_characters == 17
    assert breakdown.system_prompt_characters == len("system prompt")
    assert breakdown.user_message_characters == len('{"evidence":"material evidence"}')
    assert breakdown.serialized_request_characters + 17 == exact_limit
    assert calls == []


def test_bounded_inference_agent_records_unavailable_usage_on_provider_failure() -> None:
    sink = _CallSink()
    provider_error = RuntimeError("provider detail")

    def fail(_message: str) -> tuple[_Response, InferenceUsage]:
        raise provider_error

    tracking = InferenceTracking(sink)
    agent = BoundedInferenceAgent[_Request, _Response].create(
        invoke=fail,
        system_prompt="system prompt",
        output_type=_Response,
        model_name="provider:model",
        stage="A2",
        purpose="test_failure",
        tracking=tracking,
    )

    with pytest.raises(InferenceInvocationError) as raised, tracking.scope(run_id="run-1", work_unit_id="unit-1"):
        _ = agent(_Request(evidence="material evidence"))

    assert raised.value.__cause__ is provider_error
    assert len(sink.records) == 1
    assert sink.records[0].usage is None


def test_bounded_inference_agent_records_sdk_usage_retained_on_provider_failure() -> None:
    sink = _CallSink()
    usage = InferenceUsage(input_tokens=13, output_tokens=2, request_count=2)

    def fail(_message: str) -> tuple[_Response, InferenceUsage]:
        raise ProviderInferenceError(usage=usage, failure_kind="UnexpectedModelBehavior")

    tracking = InferenceTracking(sink)
    agent = BoundedInferenceAgent[_Request, _Response].create(
        invoke=fail,
        system_prompt="system prompt",
        output_type=_Response,
        model_name="provider:model",
        stage="A2",
        purpose="test_failure",
        tracking=tracking,
    )

    with pytest.raises(InferenceInvocationError):
        _ = agent(_Request(evidence="material evidence"))

    assert sink.records[0].usage == usage
