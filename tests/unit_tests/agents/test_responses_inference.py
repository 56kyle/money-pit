from typing import TYPE_CHECKING
from typing import ClassVar
from typing import cast

import pytest
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai import NativeOutput
from pydantic_ai.usage import RunUsage

from money_pit.agents import discovery
from money_pit.agents import interpretation
from money_pit.agents import research_planner
from money_pit.agents import synthesis
from money_pit.agents.inference import InferenceStage
from money_pit.agents.inference import InferenceUsage
from money_pit.agents.inference import NullInferenceUsageSink
from money_pit.agents.inference import ProviderInferenceError
from money_pit.agents.inference import invoke_native_output
from money_pit.agents.models import _prompt_cache_key  # pyright: ignore[reportPrivateUsage]
from money_pit.agents.models import openai_responses_settings
from money_pit.contracts import DiscoveryDraft
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import ResearchRoundPlan
from money_pit.contracts import SynthesisDraft
from money_pit.secrets import OpenAICredentials


if TYPE_CHECKING:
    from collections.abc import Callable


class _OutputA(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    accepted: bool


class _OutputB(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    reason: str


class _Result:
    output: _OutputA = _OutputA(accepted=True)


class _SuccessfulAgent:
    def run_sync(self, message: str, *, usage: RunUsage) -> _Result:
        assert message == '{"request":true}'
        usage.requests += 2
        usage.input_tokens += 21
        usage.cache_read_tokens += 8
        usage.output_tokens += 3
        return _Result()


class _FailingAgent:
    def __init__(self, *, reports_usage: bool) -> None:
        self.reports_usage: bool = reports_usage

    def run_sync(self, message: str, *, usage: RunUsage) -> _Result:
        del message
        if self.reports_usage:
            usage.requests += 2
            usage.input_tokens += 13
        raise RuntimeError("provider response containing sensitive material")


def test_invoke_native_output_aggregates_sdk_retry_usage() -> None:
    agent = cast("Agent[None, _OutputA]", cast("object", _SuccessfulAgent()))

    output, usage = invoke_native_output(agent, '{"request":true}')

    assert (output, usage) == (
        _OutputA(accepted=True),
        InferenceUsage(input_tokens=21, cache_read_tokens=8, output_tokens=3, request_count=2),
    )


def test_invoke_native_output_retains_sdk_usage_on_failure() -> None:
    agent = cast("Agent[None, _OutputA]", cast("object", _FailingAgent(reports_usage=True)))

    with pytest.raises(ProviderInferenceError) as raised:
        _ = invoke_native_output(agent, "request")

    assert raised.value.usage == InferenceUsage(input_tokens=13, request_count=2)


def test_invoke_native_output_leaves_unavailable_usage_as_original_failure() -> None:
    agent = cast("Agent[None, _OutputA]", cast("object", _FailingAgent(reports_usage=False)))

    with pytest.raises(RuntimeError) as raised:
        _ = invoke_native_output(agent, "request")

    assert not isinstance(raised.value, ProviderInferenceError)


def test_openai_responses_settings_are_stateless_and_request_independent() -> None:
    first = openai_responses_settings(
        stage="A1",
        model_name="gpt-test",
        instructions="fixed instructions",
        output_type=_OutputA,
    )
    second = openai_responses_settings(
        stage="A1",
        model_name="gpt-test",
        instructions="fixed instructions",
        output_type=_OutputA,
    )

    assert first == second
    assert first.get("openai_store") is False
    assert first.get("openai_reasoning_effort") == "medium"
    assert "openai_previous_response_id" not in first


@pytest.mark.parametrize(
    "changed_key",
    [
        pytest.param(
            lambda: _prompt_cache_key(
                stage="A2", model_name="gpt-test", instructions="fixed instructions", output_type=_OutputA
            ),
            id="stage",
        ),
        pytest.param(
            lambda: _prompt_cache_key(
                stage="A1", model_name="gpt-other", instructions="fixed instructions", output_type=_OutputA
            ),
            id="model",
        ),
        pytest.param(
            lambda: _prompt_cache_key(
                stage="A1", model_name="gpt-test", instructions="other instructions", output_type=_OutputA
            ),
            id="instructions",
        ),
        pytest.param(
            lambda: _prompt_cache_key(
                stage="A1", model_name="gpt-test", instructions="fixed instructions", output_type=_OutputB
            ),
            id="schema",
        ),
    ],
)
def test__prompt_cache_key_is_bound_to_each_stable_prefix_component(changed_key: "Callable[[], str]") -> None:
    baseline = _prompt_cache_key(
        stage="A1",
        model_name="gpt-test",
        instructions="fixed instructions",
        output_type=_OutputA,
    )

    assert changed_key() != baseline


@pytest.mark.parametrize(
    ("module", "factory", "stage", "output_type"),
    [
        pytest.param(interpretation, interpretation.make_interpretation_agent, "A1", InterpretationDraft, id="A1"),
        pytest.param(discovery, discovery.make_discovery_agent, "A2", DiscoveryDraft, id="A2"),
        pytest.param(research_planner, research_planner.make_research_planning_agent, "A3", ResearchRoundPlan, id="A3"),
        pytest.param(synthesis, synthesis.make_synthesis_agent, "A4", SynthesisDraft, id="A4"),
    ],
)
def test_make_agent_uses_responses_instructions_and_strict_native_output(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    factory: "Callable[..., object]",
    stage: InferenceStage,
    output_type: type[BaseModel],
) -> None:
    observed: dict[str, object] = {}
    provider_model = object()

    def capture_agent(**kwargs: object) -> object:
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(module, "Agent", capture_agent)

    def provider(*_args: object, **_kwargs: object) -> object:
        return provider_model

    monkeypatch.setattr(module, "openai_responses_model", provider)

    _ = factory(
        OpenAICredentials(api_key=SecretStr("test-key")),
        model="gpt-test",
        usage_sink=NullInferenceUsageSink(),
    )

    native_output = observed["output_type"]
    assert observed["model"] is provider_model
    assert observed["instructions"]
    assert isinstance(native_output, NativeOutput)
    assert native_output.outputs is output_type
    assert native_output.strict is True
    assert observed["model_settings"] == openai_responses_settings(
        stage=stage,
        model_name="gpt-test",
        instructions=str(observed["instructions"]),
        output_type=output_type,
    )
