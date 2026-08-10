from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest
import tomllib
from pydantic import SecretStr
from pydantic import ValidationError

from money_pit.agents.models import openai_chat_model
from money_pit.config import ExecutionConfig
from money_pit.config import StrategyConfig
from money_pit.config import StrategyIntelligenceConfig
from money_pit.config import canonical_config_hash
from money_pit.evidence.media import OpenAIVisionFrameReader
from money_pit.portfolio.runtime import ConfiguredTaxLotProvider
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.secrets import InferenceCredentialResolver
from money_pit.secrets import OpenAICredentials
from money_pit.secrets import YouTubeDiscoveryCredentials
from money_pit.sources.builtin import builtin_adapter_registry
from money_pit.sources.local import LocalFileConnector
from money_pit.sources.youtube import YouTubeConnector
from money_pit.sources.youtube import YouTubeConnectorConfig


if TYPE_CHECKING:
    from money_pit.secrets import SourceCredentialResolver


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
        line
        for line in (_EXAMPLES / filename).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )

    assert not any(placeholder in line for line in active_lines for placeholder in _ACTIVE_PLACEHOLDERS)


def test_openai_chat_model_uses_explicit_credentials_and_strategy_model() -> None:
    model = openai_chat_model(OpenAICredentials(api_key=SecretStr("explicit-key")), model_name="configured-model")
    provider = model.provider
    if provider is None:
        pytest.fail("OpenAI chat model did not preserve its explicit provider")

    assert (model.model_name, provider.client.api_key) == ("configured-model", "explicit-key")


def test_openai_vision_frame_reader_resolves_credentials_lazily() -> None:
    resolutions: list[str] = []

    def load_credentials() -> OpenAICredentials:
        resolutions.append("inference")
        return OpenAICredentials(api_key=SecretStr("frame-key"))

    reader = OpenAIVisionFrameReader(load_credentials, "frame-model")
    assert resolutions == []

    _ = reader._inference_agent()  # pyright: ignore[reportPrivateUsage]  # Pins the lazy provider-construction seam.

    assert resolutions == ["inference"]


class _YouTubeCredentials:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def youtube_discovery(self, *, reason: str) -> YouTubeDiscoveryCredentials:
        self.reasons.append(reason)
        return YouTubeDiscoveryCredentials(
            youtube_api_key=SecretStr("youtube-key"),
        )


class _InferenceCredentials:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def openai(self, *, reason: str) -> OpenAICredentials:
        self.reasons.append(reason)
        return OpenAICredentials(api_key=SecretStr("frame-key"))


class _RejectSourceCredentials:
    def imap(self, *, reason: str) -> None:
        raise AssertionError(f"non-credential source path resolved IMAP credentials: {reason}")

    def youtube_discovery(self, *, reason: str) -> None:
        raise AssertionError(f"non-credential source path resolved YouTube credentials: {reason}")


def test_builtin_adapter_registry_resolves_youtube_only_when_connector_is_requested() -> None:
    sources = SourceRegistryDocument.model_validate(_toml(_EXAMPLES / "sources.example.toml"))
    strategy = StrategyIntelligenceConfig.model_validate(_toml(_EXAMPLES / "strategy.example.toml"))
    definition = next(source for source in sources.sources if source.adapter_name == "youtube")
    credentials = _YouTubeCredentials()
    inference_credentials = _InferenceCredentials()
    # The focused test double intentionally implements only the capability exercised by this factory.
    credential_resolver = cast("SourceCredentialResolver", cast("object", credentials))
    inference_resolver = cast("InferenceCredentialResolver", cast("object", inference_credentials))
    registry = builtin_adapter_registry(
        credential_resolver,
        strategy,
        inference_credentials=inference_resolver,
    )

    connector = registry.create(definition)
    assert isinstance(connector, YouTubeConnector)
    assert (credentials.reasons, inference_credentials.reasons) == ([], [])

    _ = connector._api_key()  # pyright: ignore[reportPrivateUsage]  # Pins lazy discovery authority.

    assert (credentials.reasons, inference_credentials.reasons) == (
        [f"Discover uploads for YouTube source {definition.source_id}"],
        [],
    )


def test_builtin_adapter_registry_noncredential_source_does_not_resolve_imap_or_youtube() -> None:
    sources = SourceRegistryDocument.model_validate(_toml(_EXAMPLES / "sources.example.toml"))
    strategy = StrategyIntelligenceConfig.model_validate(_toml(_EXAMPLES / "strategy.example.toml"))
    definition = sources.sources[0].model_copy(
        update={"source_id": "local", "adapter_name": "local_text", "locator": "local.txt", "adapter_config": {}}
    )
    credentials = cast("SourceCredentialResolver", cast("object", _RejectSourceCredentials()))

    connector = builtin_adapter_registry(credentials, strategy).create(definition)

    assert isinstance(connector, LocalFileConnector)


def test_youtube_connector_config_has_no_ambient_environment_contract() -> None:
    assert "api_key_env" not in YouTubeConnectorConfig.model_fields


def test_strategy_model_participates_in_intelligence_configuration_hash() -> None:
    intelligence = StrategyIntelligenceConfig.model_validate(_toml(_EXAMPLES / "strategy.example.toml"))
    changed = intelligence.model_copy(update={"llm_model": "different-model"})

    assert canonical_config_hash(intelligence) != canonical_config_hash(changed)


def test_tax_lot_example_preserves_explicit_unknown_state() -> None:
    snapshot = ConfiguredTaxLotProvider(_EXAMPLES / "tax-lots.example.json").snapshot()

    assert (snapshot.complete_for_known_accounts, snapshot.unknown_external_activity) == (False, True)
