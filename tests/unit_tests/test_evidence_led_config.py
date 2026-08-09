from pathlib import Path

import pytest
import tomllib
from dotenv import dotenv_values
from pydantic import SecretStr
from pydantic import ValidationError

from money_pit.agents.models import openai_chat_model
from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.config import ExecutionConfig
from money_pit.config import StrategyConfig
from money_pit.evidence.media import OpenAIVisionFrameReader
from money_pit.portfolio.runtime import ConfiguredTaxLotProvider
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.sources.builtin import builtin_adapter_registry
from money_pit.sources.youtube import YouTubeConnector
from money_pit.sources.youtube import YouTubeConnectorConfig


_REPOSITORY_ROOT = Path(__file__).parents[2]
_EXAMPLES = _REPOSITORY_ROOT / "config" / "examples"
_ACTIVE_PLACEHOLDERS = ("CHANGE_ME", "REPLACE_ME", "<placeholder>", "TODO")


def _toml(path: Path) -> object:
    return tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("filename", "model_type"),
    [
        pytest.param("sources.example.toml", SourceRegistryDocument, id="sources"),
        pytest.param("strategy.example.toml", StrategyConfig, id="strategy"),
        pytest.param("execution.example.toml", ExecutionConfig, id="execution"),
    ],
)
def test_configured_examples_parse_as_complete_documents(
    filename: str,
    model_type: type[SourceRegistryDocument] | type[StrategyConfig] | type[ExecutionConfig],
) -> None:
    parsed = model_type.model_validate(_toml(_EXAMPLES / filename))

    assert parsed is not None


def test_strategy_benchmark_is_evaluation_metadata_without_target_weights() -> None:
    strategy = StrategyConfig.model_validate(_toml(_EXAMPLES / "strategy.example.toml"))

    assert (
        strategy.benchmark_weights,
        "strategic_core_targets" in StrategyConfig.model_fields,
        "satellite_weight_limit" in StrategyConfig.model_fields,
    ) == ({"VTI": 1.0}, False, False)


def test_strategy_requires_exposure_classification_for_every_configured_instrument() -> None:
    strategy = StrategyConfig.model_validate(_toml(_EXAMPLES / "strategy.example.toml"))
    incomplete_exposure_classes = dict(strategy.instrument_exposure_classes)
    _ = incomplete_exposure_classes.pop("GOOG")
    values = strategy.model_dump()
    values["instrument_exposure_classes"] = incomplete_exposure_classes

    with pytest.raises(ValidationError):
        _ = StrategyConfig.model_validate(values)


@pytest.mark.parametrize(
    "filename",
    ["sources.example.toml", "strategy.example.toml", "execution.example.toml", "tax-lots.example.json"],
)
def test_configured_examples_contain_no_active_placeholders(filename: str) -> None:
    active_lines = tuple(
        line for line in (_EXAMPLES / filename).read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )

    assert not any(placeholder in line for line in active_lines for placeholder in _ACTIVE_PLACEHOLDERS)


def test_env_example_configures_the_namespaced_model_without_fake_credentials() -> None:
    values = dotenv_values(_EXAMPLES / ".env.example")

    assert values == {"MONEY_PIT__LLM_MODEL": "gpt-5.6-sol"}


def test_openai_chat_model_uses_the_namespaced_key_without_ambient_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = openai_chat_model(
        Config(openai_api_key=SecretStr("namespaced-key"), llm_model="configured-model")
    )
    provider = model.provider
    if provider is None:
        pytest.fail("OpenAI chat model did not preserve its explicit provider")

    assert (model.model_name, provider.client.api_key) == ("configured-model", "namespaced-key")


def test_openai_chat_model_with_missing_key_raises_typed_error() -> None:
    with pytest.raises(CredentialResolutionError):
        _ = openai_chat_model(Config())


def test_openai_vision_frame_reader_resolves_credentials_lazily() -> None:
    reader = OpenAIVisionFrameReader(Config())

    with pytest.raises(CredentialResolutionError):
        _ = reader._inference_agent()  # pyright: ignore[reportPrivateUsage]  # Pins the lazy provider-construction seam.


def test_builtin_adapter_registry_resolves_the_config_bound_youtube_key_lazily() -> None:
    sources = SourceRegistryDocument.model_validate(_toml(_EXAMPLES / "sources.example.toml"))
    definition = next(source for source in sources.sources if source.adapter_name == "youtube")
    registry = builtin_adapter_registry(Config(youtube_api_key=SecretStr("namespaced-youtube-key")))

    connector = registry.create(definition)

    assert isinstance(connector, YouTubeConnector)
    assert connector._api_key.get_secret_value() == "namespaced-youtube-key"  # pyright: ignore[reportPrivateUsage]


def test_builtin_adapter_registry_with_missing_youtube_key_fails_only_when_requested() -> None:
    sources = SourceRegistryDocument.model_validate(_toml(_EXAMPLES / "sources.example.toml"))
    definition = next(source for source in sources.sources if source.adapter_name == "youtube")
    registry = builtin_adapter_registry(Config())

    with pytest.raises(CredentialResolutionError):
        _ = registry.create(definition)


def test_youtube_connector_config_has_no_ambient_environment_contract() -> None:
    assert "api_key_env" not in YouTubeConnectorConfig.model_fields


def test_tax_lot_example_preserves_explicit_unknown_state() -> None:
    snapshot = ConfiguredTaxLotProvider(_EXAMPLES / "tax-lots.example.json").snapshot()

    assert (snapshot.complete_for_known_accounts, snapshot.unknown_external_activity) == (False, True)
