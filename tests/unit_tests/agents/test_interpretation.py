from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from pydantic import SecretStr
from pydantic_ai.usage import RunUsage

from money_pit.agents import interpretation as interpretation_module
from money_pit.agents.inference import InferenceInvocationContext
from money_pit.agents.inference import InferenceInvocationError
from money_pit.agents.inference import NullInferenceUsageSink
from money_pit.contracts import ClaimObservationDraft
from money_pit.contracts import EvidencePromptRecord
from money_pit.contracts import InterpretationAgent
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import InterpretationRequest
from money_pit.prompt_loader import system_prompt
from money_pit.secrets import OpenAICredentials


_REQUESTED_AS_OF = datetime(2026, 8, 7, 17, 0, tzinfo=UTC)
_CONTEXT = InferenceInvocationContext(run_id="run-1", work_unit_id="attempt-1")
_CONTEXT_KNOWN_AT = _REQUESTED_AS_OF + timedelta(milliseconds=99)
_NAIVE_OUTPUT = """{
  "observations": [{
    "claim_text": "The release is scheduled.",
    "claim_kind": "factual",
    "category": "macro",
    "evidence_aliases": ["E000001"],
    "asserted_at": "2026-08-07T16:30:00",
    "event_at": "2026-08-07T16:30:00",
    "horizon_class": "event"
  }]
}"""
_VALID_OUTPUT = """{
  "observations": [{
    "claim_text": "The release is scheduled.",
    "claim_kind": "factual",
    "category": "macro",
    "evidence_aliases": ["E000001"],
    "asserted_at": "2026-08-07T16:30:00-04:00",
    "event_at": "2026-08-07T16:30:00-04:00",
    "horizon_class": "event"
  }]
}"""


def _request() -> InterpretationRequest:
    return InterpretationRequest(
        source_item_id="source:item",
        evidence=(
            EvidencePromptRecord(
                alias="E000001",
                kind="transcript",
                text="The release is scheduled for 4:30 PM EDT on August 7, 2026.",
                core=True,
            ),
        ),
        requested_as_of=_REQUESTED_AS_OF,
        context_known_at=_CONTEXT_KNOWN_AT,
    )


def _field_description(field_name: str) -> str:
    description = ClaimObservationDraft.model_fields[field_name].description
    assert description is not None
    return description.lower()


def _agent_with_outputs(
    monkeypatch: pytest.MonkeyPatch,
    outputs: list[str],
) -> tuple[InterpretationAgent, list[str]]:
    rendered_requests: list[str] = []

    class CoreAgent:
        def run_sync(self, message: str, *, usage: RunUsage) -> object:
            for attempt in range(2):
                rendered_requests.append(message)
                output = outputs.pop(0)
                usage.requests += 1
                usage.input_tokens += 10
                usage.cache_write_tokens += 2
                usage.cache_read_tokens += 3
                usage.output_tokens += 4
                try:
                    parsed = InterpretationDraft.model_validate_json(output)
                except ValueError:
                    if attempt == 0:
                        continue
                    raise
                return type("Result", (), {"output": parsed})()
            raise AssertionError("unreachable")

    def construct_agent(**kwargs: object) -> CoreAgent:
        del kwargs
        return CoreAgent()

    def construct_model(
        credentials: OpenAICredentials,
        *,
        model_name: str,
    ) -> object:
        del credentials, model_name
        return object()

    monkeypatch.setattr(interpretation_module, "Agent", construct_agent)
    monkeypatch.setattr(interpretation_module, "openai_responses_model", construct_model)
    agent = interpretation_module.make_interpretation_agent(
        OpenAICredentials(api_key=SecretStr("test-key")),
        model="test-model",
        usage_sink=NullInferenceUsageSink(),
    )
    return agent, rendered_requests


def test_interpretation_system_prompt_requires_offset_aware_economic_times() -> None:
    prompt = system_prompt("interpretation").lower()

    assert all(
        requirement in prompt
        for requirement in (
            "rfc 3339",
            "explicit utc offset",
            "requested_as_of",
            "null",
        )
    )


def test_interpretation_system_prompt_does_not_use_later_context_as_assertion_fallback() -> None:
    prompt = system_prompt("interpretation").lower()

    assert "context_known_at" not in prompt


@pytest.mark.parametrize(
    "field_name",
    ["asserted_at", "effective_from", "event_at", "review_at", "valid_until"],
)
def test_claim_observation_draft_datetime_schema_requires_an_explicit_offset(
    field_name: str,
) -> None:
    description = _field_description(field_name)

    assert all(snippet in description for snippet in ("rfc 3339", "explicit utc offset"))


def test_claim_observation_draft_asserted_at_schema_defines_conservative_fallback() -> None:
    description = _field_description("asserted_at")

    assert all(snippet in description for snippet in ("source", "requested_as_of"))


def test_claim_observation_draft_asserted_at_schema_does_not_use_later_context_fallback() -> None:
    description = _field_description("asserted_at")

    assert "context_known_at" not in description


@pytest.mark.parametrize(
    "field_name",
    ["effective_from", "event_at", "review_at", "valid_until"],
)
def test_claim_observation_draft_optional_economic_time_schema_requires_evidenced_timezone(
    field_name: str,
) -> None:
    description = _field_description(field_name)

    assert all(snippet in description for snippet in ("null", "evidence", "timezone"))


def test_make_interpretation_agent_retries_one_validation_error_with_identical_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, rendered_requests = _agent_with_outputs(monkeypatch, [_NAIVE_OUTPUT, _VALID_OUTPUT])

    result = agent(_request(), context=_CONTEXT)

    assert (result.output, result.usage.request_count, rendered_requests) == (
        InterpretationDraft.model_validate_json(_VALID_OUTPUT),
        2,
        [rendered_requests[0], rendered_requests[0]],
    )


def test_make_interpretation_agent_propagates_second_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, rendered_requests = _agent_with_outputs(monkeypatch, [_NAIVE_OUTPUT, _NAIVE_OUTPUT])

    with pytest.raises(InferenceInvocationError) as captured:
        _ = agent(_request(), context=_CONTEXT)

    assert (captured.value.failure_kind, len(rendered_requests)) == ("ValidationError", 2)
