from pathlib import Path

import pytest
import tomllib
from pydantic import SecretStr
from pytest import MonkeyPatch

from money_pit.composition import build_production_application_dependencies
from money_pit.config import ApplicationConfig
from money_pit.config import StrategyIntelligenceConfig
from money_pit.pipeline.chain import Stage
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.secrets import AlpacaCredentials
from money_pit.secrets import BraveCredentials
from money_pit.secrets import CredentialResolutionError
from money_pit.secrets import EdgarCredentials
from money_pit.secrets import FredCredentials
from money_pit.secrets import ImapCredentials
from money_pit.secrets import OpenAICredentials
from money_pit.secrets import SecretSpecConfigurationError
from money_pit.secrets import SecretSpecExecutionResolver
from money_pit.secrets import SecretSpecInferenceResolver
from money_pit.secrets import SecretSpecPortfolioResolver
from money_pit.secrets import SecretSpecResearchResolver
from money_pit.secrets import SecretSpecResolver
from money_pit.secrets import SecretSpecSourceResolver
from money_pit.secrets import YouTubeMediaCredentials
from money_pit.storage.database import Database


_REPOSITORY_ROOT = Path(__file__).parents[2]
_SECRET_NAMES = (
    "OPENAI_API_KEY",
    "YOUTUBE_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "FRED_API_KEY",
    "SEC_USER_AGENT",
    "IMAP_USERNAME",
    "IMAP_PASSWORD",
    "ALPACA_PAPER_API_KEY",
    "ALPACA_PAPER_SECRET_KEY",
    "ALPACA_LIVE_API_KEY",
    "ALPACA_LIVE_SECRET_KEY",
)
_DUMMY_SECRETS = {name: f"dummy-{name.lower()}" for name in _SECRET_NAMES}


@pytest.fixture
def secretspec_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "secretspec.toml"
    _ = manifest.write_text((_REPOSITORY_ROOT / "secretspec.toml").read_text(encoding="utf-8"), encoding="utf-8")
    return manifest


@pytest.fixture
def secret_environment(monkeypatch: MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    for name, value in _DUMMY_SECRETS.items():
        monkeypatch.setenv(name, value)
    return _DUMMY_SECRETS


@pytest.fixture
def inference_resolver(secretspec_manifest: Path, secret_environment: dict[str, str]) -> SecretSpecInferenceResolver:
    del secret_environment
    return SecretSpecInferenceResolver(secretspec_manifest, profile="ci")


@pytest.fixture
def source_resolver(secretspec_manifest: Path, secret_environment: dict[str, str]) -> SecretSpecSourceResolver:
    del secret_environment
    return SecretSpecSourceResolver(secretspec_manifest, profile="ci")


@pytest.fixture
def research_resolver(secretspec_manifest: Path, secret_environment: dict[str, str]) -> SecretSpecResearchResolver:
    del secret_environment
    return SecretSpecResearchResolver(secretspec_manifest, profile="ci")


@pytest.fixture
def portfolio_resolver(secretspec_manifest: Path, secret_environment: dict[str, str]) -> SecretSpecPortfolioResolver:
    del secret_environment
    return SecretSpecPortfolioResolver(secretspec_manifest, profile="ci")


@pytest.fixture
def execution_resolver(secretspec_manifest: Path, secret_environment: dict[str, str]) -> SecretSpecExecutionResolver:
    del secret_environment
    return SecretSpecExecutionResolver(secretspec_manifest, profile="ci")


@pytest.fixture
def later_research_config() -> ApplicationConfig:
    sources = SourceRegistryDocument.model_validate(
        tomllib.loads((_REPOSITORY_ROOT / "config" / "examples" / "sources.example.toml").read_text(encoding="utf-8"))
    )
    enabled_sources = tuple(
        source.model_copy(update={"enabled": True})
        if {"research-provider", "research-publisher"}.intersection(source.tags)
        else source
        for source in sources.sources
    )
    strategy_document = tomllib.loads(
        (_REPOSITORY_ROOT / "config" / "examples" / "strategy.example.toml").read_text(encoding="utf-8")
    )
    return ApplicationConfig(
        sources=sources.model_copy(update={"sources": enabled_sources}),
        intelligence=StrategyIntelligenceConfig.model_validate(strategy_document),
        strategy=None,
        execution=None,
    )


@pytest.fixture
def inference_only_resolver(
    secretspec_manifest: Path,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> SecretSpecInferenceResolver:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    for name in _SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-inference-only-key")
    return SecretSpecInferenceResolver(secretspec_manifest, profile="ci")


@pytest.fixture
def missing_research_resolver(
    secretspec_manifest: Path,
    inference_only_resolver: SecretSpecInferenceResolver,
) -> SecretSpecResearchResolver:
    del inference_only_resolver
    return SecretSpecResearchResolver(secretspec_manifest, profile="ci")


def test_secret_spec_resolver_openai_scope(
    inference_resolver: SecretSpecInferenceResolver,
    secret_environment: dict[str, str],
) -> None:
    assert inference_resolver.openai(reason="test inference") == OpenAICredentials(
        api_key=SecretStr(secret_environment["OPENAI_API_KEY"])
    )


def test_secret_spec_resolver_youtube_media_scope(
    source_resolver: SecretSpecSourceResolver,
    secret_environment: dict[str, str],
) -> None:
    assert source_resolver.youtube_media(reason="test media") == YouTubeMediaCredentials(
        youtube_api_key=SecretStr(secret_environment["YOUTUBE_API_KEY"]),
        openai_api_key=SecretStr(secret_environment["OPENAI_API_KEY"]),
    )


def test_secret_spec_resolver_imap_scope(
    source_resolver: SecretSpecSourceResolver,
    secret_environment: dict[str, str],
) -> None:
    assert source_resolver.imap(reason="test imap") == ImapCredentials(
        username=SecretStr(secret_environment["IMAP_USERNAME"]),
        password=SecretStr(secret_environment["IMAP_PASSWORD"]),
    )


def test_secret_spec_resolver_brave_scope(
    research_resolver: SecretSpecResearchResolver,
    secret_environment: dict[str, str],
) -> None:
    assert research_resolver.brave(reason="test brave") == BraveCredentials(
        api_key=SecretStr(secret_environment["BRAVE_SEARCH_API_KEY"])
    )


def test_secret_spec_resolver_edgar_scope(
    research_resolver: SecretSpecResearchResolver,
    secret_environment: dict[str, str],
) -> None:
    assert research_resolver.edgar(reason="test edgar") == EdgarCredentials(
        user_agent=SecretStr(secret_environment["SEC_USER_AGENT"])
    )


def test_secret_spec_resolver_fred_scope(
    research_resolver: SecretSpecResearchResolver,
    secret_environment: dict[str, str],
) -> None:
    assert research_resolver.fred(reason="test fred") == FredCredentials(
        api_key=SecretStr(secret_environment["FRED_API_KEY"])
    )


@pytest.mark.parametrize("through", [Stage.A1, Stage.A2])
def test_build_production_application_dependencies_defers_later_research_credentials(
    tmp_path: Path,
    later_research_config: ApplicationConfig,
    inference_only_resolver: SecretSpecInferenceResolver,
    missing_research_resolver: SecretSpecResearchResolver,
    through: Stage,
) -> None:
    database = Database(tmp_path / "stage.sqlite3")
    database.initialize()

    dependencies = build_production_application_dependencies(
        database=database,
        config=later_research_config,
        reports_root=tmp_path / "reports",
        implementation_version="test-stage-authority",
        through=through,
        inference_credentials=inference_only_resolver,
        research_credentials=missing_research_resolver,
    )

    assert dependencies.research_providers.names() == ()


def test_build_production_application_dependencies_resolves_research_credentials_at_a3(
    tmp_path: Path,
    later_research_config: ApplicationConfig,
    inference_only_resolver: SecretSpecInferenceResolver,
    missing_research_resolver: SecretSpecResearchResolver,
) -> None:
    database = Database(tmp_path / "stage.sqlite3")
    database.initialize()

    with pytest.raises(CredentialResolutionError):
        _ = build_production_application_dependencies(
            database=database,
            config=later_research_config,
            reports_root=tmp_path / "reports",
            implementation_version="test-stage-authority",
            through=Stage.A3,
            inference_credentials=inference_only_resolver,
            research_credentials=missing_research_resolver,
        )


@pytest.mark.parametrize(
    ("capability", "environment", "api_key_name", "secret_key_name"),
    [
        pytest.param(
            "portfolio",
            BrokerEnvironment.PAPER,
            "ALPACA_PAPER_API_KEY",
            "ALPACA_PAPER_SECRET_KEY",
            id="portfolio-paper",
        ),
        pytest.param(
            "portfolio",
            BrokerEnvironment.LIVE,
            "ALPACA_LIVE_API_KEY",
            "ALPACA_LIVE_SECRET_KEY",
            id="portfolio-live",
        ),
        pytest.param(
            "execution",
            BrokerEnvironment.PAPER,
            "ALPACA_PAPER_API_KEY",
            "ALPACA_PAPER_SECRET_KEY",
            id="execution-paper",
        ),
        pytest.param(
            "execution",
            BrokerEnvironment.LIVE,
            "ALPACA_LIVE_API_KEY",
            "ALPACA_LIVE_SECRET_KEY",
            id="execution-live",
        ),
    ],
)
def test_secret_spec_resolver_keeps_alpaca_environments_separate(
    portfolio_resolver: SecretSpecPortfolioResolver,
    execution_resolver: SecretSpecExecutionResolver,
    secret_environment: dict[str, str],
    capability: str,
    environment: BrokerEnvironment,
    api_key_name: str,
    secret_key_name: str,
) -> None:
    credentials: AlpacaCredentials = (
        portfolio_resolver.alpaca_portfolio(environment, reason="test portfolio")
        if capability == "portfolio"
        else execution_resolver.alpaca_execution(environment, reason="test execution")
    )

    assert (
        credentials.api_key.get_secret_value(),
        credentials.secret_key.get_secret_value(),
        credentials.broker_environment,
    ) == (secret_environment[api_key_name], secret_environment[secret_key_name], environment)


def test_secret_spec_resolver_with_missing_capability_credential_is_typed_and_sanitized(
    secretspec_manifest: Path,
    secret_environment: dict[str, str],
    monkeypatch: MonkeyPatch,
) -> None:
    missing_value = secret_environment["BRAVE_SEARCH_API_KEY"]
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY")

    with pytest.raises(CredentialResolutionError) as captured:
        _ = SecretSpecResearchResolver(secretspec_manifest, profile="ci").brave(reason="test missing")

    assert missing_value not in str(captured.value)


def test_secret_spec_resolver_with_unknown_profile_is_configuration_error(
    secretspec_manifest: Path,
    secret_environment: dict[str, str],
) -> None:
    del secret_environment

    with pytest.raises(SecretSpecConfigurationError):
        _ = SecretSpecInferenceResolver(secretspec_manifest, profile="unknown").openai(reason="test profile")


def test_secret_spec_resolver_from_environment_selects_ci_profile(
    secretspec_manifest: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRETSPEC_PROFILE", "ci")

    resolver = SecretSpecResolver.from_environment(secretspec_manifest)

    assert resolver.profile == "ci"


def test_secret_spec_resolver_from_environment_selects_development_when_unset(
    secretspec_manifest: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("SECRETSPEC_PROFILE", raising=False)

    resolver = SecretSpecResolver.from_environment(secretspec_manifest)

    assert resolver.profile == "development"


def test_secret_spec_resolver_from_environment_rejects_blank_profile(
    secretspec_manifest: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRETSPEC_PROFILE", " ")

    with pytest.raises(CredentialResolutionError):
        _ = SecretSpecResolver.from_environment(secretspec_manifest)


def test_secret_spec_resolver_with_malformed_manifest_is_configuration_error(
    tmp_path: Path,
    secret_environment: dict[str, str],
) -> None:
    del secret_environment
    manifest = tmp_path / "malformed.toml"
    _ = manifest.write_text("[project\n", encoding="utf-8")

    with pytest.raises(SecretSpecConfigurationError):
        _ = SecretSpecInferenceResolver(manifest, profile="ci").openai(reason="test manifest")


def test_secret_spec_resolver_does_not_disclose_values_in_credential_representations(
    inference_resolver: SecretSpecInferenceResolver,
    source_resolver: SecretSpecSourceResolver,
    research_resolver: SecretSpecResearchResolver,
    portfolio_resolver: SecretSpecPortfolioResolver,
    execution_resolver: SecretSpecExecutionResolver,
    secret_environment: dict[str, str],
) -> None:
    credentials = (
        inference_resolver.openai(reason="test inference"),
        source_resolver.youtube_media(reason="test media"),
        source_resolver.imap(reason="test imap"),
        research_resolver.brave(reason="test brave"),
        research_resolver.edgar(reason="test edgar"),
        research_resolver.fred(reason="test fred"),
        portfolio_resolver.alpaca_portfolio(BrokerEnvironment.PAPER, reason="test portfolio"),
        execution_resolver.alpaca_execution(BrokerEnvironment.LIVE, reason="test execution"),
    )
    rendered = repr(credentials)

    assert not any(value in rendered for value in secret_environment.values())


def test_tracked_manifest_contains_declarations_but_no_values() -> None:
    manifest_text = (_REPOSITORY_ROOT / "secretspec.toml").read_text(encoding="utf-8")
    _: object = tomllib.loads(manifest_text)

    assert "[profiles.development]" in manifest_text
    assert "[profiles.default]" not in manifest_text
    assert ("value =" in manifest_text, "value=" in manifest_text) == (False, False)
