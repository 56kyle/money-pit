from pathlib import Path
from typing import cast

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
from money_pit.secrets import YouTubeDiscoveryCredentials
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
def edgar_research_config() -> ApplicationConfig:
    sources = SourceRegistryDocument.model_validate(
        tomllib.loads((_REPOSITORY_ROOT / "config" / "examples" / "sources.example.toml").read_text(encoding="utf-8"))
    )
    enabled_sources = tuple(
        source.model_copy(update={"enabled": True})
        if {"research-provider", "provider:edgar"}.issubset(source.tags)
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
def missing_edgar_inference_resolver(
    secretspec_manifest: Path,
    secret_environment: dict[str, str],
    monkeypatch: MonkeyPatch,
) -> SecretSpecInferenceResolver:
    del secret_environment
    monkeypatch.delenv("SEC_USER_AGENT")
    return SecretSpecInferenceResolver(secretspec_manifest, profile="ci")


@pytest.fixture
def missing_edgar_research_resolver(
    secretspec_manifest: Path,
    missing_edgar_inference_resolver: SecretSpecInferenceResolver,
) -> SecretSpecResearchResolver:
    del missing_edgar_inference_resolver
    return SecretSpecResearchResolver(secretspec_manifest, profile="ci")


def test_secret_spec_resolver_openai_scope(
    inference_resolver: SecretSpecInferenceResolver,
    secret_environment: dict[str, str],
) -> None:
    assert inference_resolver.openai(reason="test inference") == OpenAICredentials(
        api_key=SecretStr(secret_environment["OPENAI_API_KEY"])
    )


def test_secret_spec_resolver_youtube_discovery_scope(
    source_resolver: SecretSpecSourceResolver,
    secret_environment: dict[str, str],
) -> None:
    assert source_resolver.youtube_discovery(reason="test discovery") == YouTubeDiscoveryCredentials(
        youtube_api_key=SecretStr(secret_environment["YOUTUBE_API_KEY"]),
    )


def test_secret_spec_resolver_imap_scope(
    source_resolver: SecretSpecSourceResolver,
    secret_environment: dict[str, str],
) -> None:
    assert source_resolver.imap(reason="test imap") == ImapCredentials(
        username=SecretStr(secret_environment["IMAP_USERNAME"]),
        password=SecretStr(secret_environment["IMAP_PASSWORD"]),
    )


@pytest.mark.parametrize(
    "present_names",
    [
        pytest.param((), id="missing-pair"),
        pytest.param(("IMAP_USERNAME",), id="username-only"),
        pytest.param(("IMAP_PASSWORD",), id="password-only"),
    ],
)
def test_secret_spec_resolver_imap_with_incomplete_pair_is_typed_and_sanitized(
    secretspec_manifest: Path,
    secret_environment: dict[str, str],
    monkeypatch: MonkeyPatch,
    present_names: tuple[str, ...],
) -> None:
    for name in ("IMAP_USERNAME", "IMAP_PASSWORD"):
        if name not in present_names:
            monkeypatch.delenv(name)

    with pytest.raises(CredentialResolutionError) as captured:
        _ = SecretSpecSourceResolver(secretspec_manifest, profile="ci").imap(reason="test incomplete pair")

    assert not any(value in str(captured.value) for value in secret_environment.values())


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        pytest.param("IMAP_USERNAME", "", id="blank-username"),
        pytest.param("IMAP_USERNAME", " \t ", id="whitespace-username"),
        pytest.param("IMAP_PASSWORD", "", id="blank-password"),
        pytest.param("IMAP_PASSWORD", " \t ", id="whitespace-password"),
    ],
)
def test_secret_spec_resolver_imap_with_blank_pair_field_is_typed_and_sanitized(
    secretspec_manifest: Path,
    secret_environment: dict[str, str],
    monkeypatch: MonkeyPatch,
    field_name: str,
    invalid_value: str,
) -> None:
    monkeypatch.setenv(field_name, invalid_value)

    with pytest.raises(CredentialResolutionError) as captured:
        _ = SecretSpecSourceResolver(secretspec_manifest, profile="ci").imap(reason="test blank pair field")

    assert not any(value in str(captured.value) for value in secret_environment.values())


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


@pytest.mark.parametrize("invalid_value", [pytest.param("", id="blank"), pytest.param(" \t ", id="whitespace")])
def test_secret_spec_resolver_edgar_with_blank_identity_is_typed_and_sanitized(
    secretspec_manifest: Path,
    secret_environment: dict[str, str],
    monkeypatch: MonkeyPatch,
    invalid_value: str,
) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", invalid_value)

    with pytest.raises(CredentialResolutionError) as captured:
        _ = SecretSpecResearchResolver(secretspec_manifest, profile="ci").edgar(reason="test blank identity")

    assert not any(value in str(captured.value) for value in secret_environment.values())


def test_secret_spec_resolver_fred_scope(
    research_resolver: SecretSpecResearchResolver,
    secret_environment: dict[str, str],
) -> None:
    assert research_resolver.fred(reason="test fred") == FredCredentials(
        api_key=SecretStr(secret_environment["FRED_API_KEY"])
    )


@pytest.mark.parametrize("through", [Stage.A1, Stage.A2])
def test_build_production_application_dependencies_defers_missing_edgar_identity_before_a3(
    tmp_path: Path,
    edgar_research_config: ApplicationConfig,
    missing_edgar_inference_resolver: SecretSpecInferenceResolver,
    missing_edgar_research_resolver: SecretSpecResearchResolver,
    through: Stage,
) -> None:
    database = Database(tmp_path / "stage.sqlite3")
    database.initialize()

    dependencies = build_production_application_dependencies(
        database=database,
        config=edgar_research_config,
        reports_root=tmp_path / "reports",
        implementation_version="test-stage-authority",
        through=through,
        inference_credentials=missing_edgar_inference_resolver,
        research_credentials=missing_edgar_research_resolver,
    )

    assert dependencies.research_providers.names() == ()


def test_build_production_application_dependencies_requires_edgar_identity_at_enabled_a3_boundary(
    tmp_path: Path,
    edgar_research_config: ApplicationConfig,
    missing_edgar_inference_resolver: SecretSpecInferenceResolver,
    missing_edgar_research_resolver: SecretSpecResearchResolver,
) -> None:
    database = Database(tmp_path / "stage.sqlite3")
    database.initialize()

    with pytest.raises(CredentialResolutionError):
        _ = build_production_application_dependencies(
            database=database,
            config=edgar_research_config,
            reports_root=tmp_path / "reports",
            implementation_version="test-stage-authority",
            through=Stage.A3,
            inference_credentials=missing_edgar_inference_resolver,
            research_credentials=missing_edgar_research_resolver,
        )


@pytest.mark.parametrize(
    "invalid_value",
    [pytest.param(None, id="missing"), pytest.param("", id="blank"), pytest.param(" \t ", id="whitespace")],
)
def test_secret_spec_resolver_youtube_discovery_with_invalid_key_is_typed_and_sanitized(
    secretspec_manifest: Path,
    secret_environment: dict[str, str],
    monkeypatch: MonkeyPatch,
    invalid_value: str | None,
) -> None:
    if invalid_value is None:
        monkeypatch.delenv("YOUTUBE_API_KEY")
    else:
        monkeypatch.setenv("YOUTUBE_API_KEY", invalid_value)

    with pytest.raises(CredentialResolutionError) as captured:
        _ = SecretSpecSourceResolver(secretspec_manifest, profile="ci").youtube_discovery(reason="test missing key")

    assert not any(value in str(captured.value) for value in secret_environment.values())


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
        source_resolver.youtube_discovery(reason="test discovery"),
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


def test_tracked_manifest_declares_ci_live_alpaca_credentials_without_merging_paper_scope() -> None:
    manifest_text = (_REPOSITORY_ROOT / "secretspec.toml").read_text(encoding="utf-8")
    manifest: dict[str, object] = tomllib.loads(manifest_text)
    profiles = cast("dict[str, dict[str, dict[str, object]]]", manifest["profiles"])
    ci_profile = profiles["ci"]
    scopes = cast("dict[str, dict[str, list[str]]]", manifest["scopes"])

    ci_live_declarations = {
        name: {"required": ci_profile[name]["required"], "providers": ci_profile[name]["providers"]}
        for name in ("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY")
    }
    live_scopes = {name: scopes[name] for name in ("portfolio_live", "execution_live")}
    paper_scopes = {name: scopes[name] for name in ("portfolio_paper", "execution_paper")}

    assert (
        ci_live_declarations,
        live_scopes,
        paper_scopes,
    ) == (
        {
            "ALPACA_LIVE_API_KEY": {"required": True, "providers": ["ci_env"]},
            "ALPACA_LIVE_SECRET_KEY": {"required": True, "providers": ["ci_env"]},
        },
        {
            "portfolio_live": {"secrets": ["ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY"]},
            "execution_live": {"secrets": ["ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY"]},
        },
        {
            "portfolio_paper": {"secrets": ["ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY"]},
            "execution_paper": {"secrets": ["ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY"]},
        },
    )


def test_root_ci_workflows_inject_no_application_credentials() -> None:
    workflows_root = _REPOSITORY_ROOT / ".github" / "workflows"
    workflow_paths = (*workflows_root.glob("*.yml"), *workflows_root.glob("*.yaml"))
    workflow_text = "\n".join(path.read_text(encoding="utf-8") for path in workflow_paths)

    assert not any(name in workflow_text for name in _SECRET_NAMES)
