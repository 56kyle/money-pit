"""Unit tests for orchestration's production wiring, its dependency builders, and its preserved fail-closed default.

production_deps composes the real capital-critical factories; its live seams (Alpaca credential resolution
and the spawning live_manifest) are monkeypatched at their orchestration import sites, and the Gmail app
password is served from a real in-memory keyring, so the wiring runs offline without connecting or spawning.
run_pipeline(overrides=None) is pinned as still failing closed with MissingPipelineDependencyError.

The *_or_default dependency builders own the override-or-default precedence that run_pipeline and any stage
runner share, so that precedence is pinned per builder: an override is returned by identity, and an empty
PipelineOverrides yields a usable default that constructs offline. The builder-family precedence tests are
parametrized over the builders themselves and split by arity, with each case's id naming the builder under
test, and a drift guard asserts those hand-maintained tables still cover every *_or_default name the module
exports.
"""

import inspect
from collections.abc import Callable
from pathlib import Path

import openai
import pytest
from pydantic import SecretStr
from pytest import MonkeyPatch

from money_pit.agents.corroboration import corroborate
from money_pit.agents.research_tools import DeterministicResearchTools
from money_pit.agents.research_tools import OpenEndedResearchTools
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.email_sender import EmailNotConfiguredError
from money_pit.pipeline import orchestration
from money_pit.pipeline.orchestration import MissingPipelineDependencyError
from money_pit.pipeline.orchestration import PipelineOverrides
from money_pit.pipeline.orchestration import answer_synthesis_agent_or_default
from money_pit.pipeline.orchestration import claim_questions_agent_or_default
from money_pit.pipeline.orchestration import corroboration_agent_or_default
from money_pit.pipeline.orchestration import deterministic_research_tools_or_default
from money_pit.pipeline.orchestration import instrument_facts_resolver_or_default
from money_pit.pipeline.orchestration import open_ended_research_tools_or_default
from money_pit.pipeline.orchestration import phase4_overrides
from money_pit.pipeline.orchestration import production_deps
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.pipeline.orchestration import thesis_agent_or_default
from tests.unit_tests.conftest import InMemoryKeyring


@pytest.fixture
def credentials() -> AlpacaCredentials:
    return AlpacaCredentials(api_key="the-key", secret_key=SecretStr("the-secret"), paper=True)


@pytest.fixture
def config() -> Config:
    return Config(
        alpaca_service="alpaca-paper",
        alpaca_username="the-key",
        alpaca_paper=True,
        gmail_address="sender@gmail.com",
    )


@pytest.fixture
def offline_production_seams(
    monkeypatch: MonkeyPatch,
    config: Config,
    credentials: AlpacaCredentials,
    in_memory_keyring: InMemoryKeyring,
) -> None:
    monkeypatch.setattr(orchestration, "resolve_alpaca_credentials", lambda _config: credentials)
    monkeypatch.setattr(orchestration, "live_manifest", lambda _credentials: {"place_stock_order": {}})
    assert config.gmail_address is not None
    in_memory_keyring.set_password(config.gmail_service, config.gmail_address, "gmail-app-password")


def test_production_deps_wires_all_capital_critical_deps(config: Config, offline_production_seams: None) -> None:
    overrides: PipelineOverrides = production_deps(config)

    assert overrides.fetch_portfolio is not None
    assert overrides.place_order is not None
    assert overrides.observe_fill is not None
    assert overrides.send_email is not None
    assert overrides.manifest is not None


@pytest.fixture
def config_without_gmail() -> Config:
    return Config(alpaca_service="alpaca-paper", alpaca_username="the-key", alpaca_paper=True, gmail_address=None)


@pytest.fixture
def offline_alpaca_seams(
    monkeypatch: MonkeyPatch, credentials: AlpacaCredentials, in_memory_keyring: InMemoryKeyring
) -> None:
    """in_memory_keyring is load-bearing: production_deps resolves the Gmail app password through the real keyring."""
    monkeypatch.setattr(orchestration, "resolve_alpaca_credentials", lambda _config: credentials)
    monkeypatch.setattr(orchestration, "live_manifest", lambda _credentials: {"place_stock_order": {}})


@pytest.fixture
def deps_without_gmail(config_without_gmail: Config, offline_alpaca_seams: None) -> PipelineOverrides:
    return production_deps(config_without_gmail)


def test_production_deps_with_unconfigured_gmail_still_composes(deps_without_gmail: PipelineOverrides) -> None:
    assert deps_without_gmail.send_email is not None
    assert deps_without_gmail.place_order is not None


def test_production_deps_with_unconfigured_gmail_send_email_raises(deps_without_gmail: PipelineOverrides) -> None:
    assert deps_without_gmail.send_email is not None

    with pytest.raises(EmailNotConfiguredError):
        deps_without_gmail.send_email("money-pit: Recovery Halt - test-run", "A prior leg is still open.")


def test_run_pipeline_with_no_overrides_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MissingPipelineDependencyError):
        _ = run_pipeline(signals_dir=tmp_path)


class _OfflineOpenEndedTools:
    """A real OpenEndedResearchTools that retrieves nothing, so open_ended_tools can be overridden offline."""

    def brave_search(self, query: str, *, n_results: int = 5) -> list[str]:
        _ = (query, n_results)
        return []

    def edgar_search(self, query: str, *, n_results: int = 5) -> list[str]:
        _ = (query, n_results)
        return []


@pytest.fixture
def fully_overridden() -> PipelineOverrides:
    """Return overrides with every builder-backed field populated by a real offline stand-in."""
    overrides: PipelineOverrides = phase4_overrides()
    overrides.open_ended_tools = _OfflineOpenEndedTools()
    return overrides


@pytest.fixture
def empty_overrides() -> PipelineOverrides:
    return PipelineOverrides()


_CONFIG_TAKING_BUILDERS: list[tuple[Callable[[PipelineOverrides, Config], object], str]] = [
    (deterministic_research_tools_or_default, "deterministic_tools"),
    (open_ended_research_tools_or_default, "open_ended_tools"),
    (thesis_agent_or_default, "thesis_agent"),
    (claim_questions_agent_or_default, "claim_questions_agent"),
    (answer_synthesis_agent_or_default, "answer_synthesis_agent"),
]

_CONFIG_FREE_BUILDERS: list[tuple[Callable[[PipelineOverrides], object], str]] = [
    (instrument_facts_resolver_or_default, "resolve_instrument_facts"),
    (corroboration_agent_or_default, "corroboration_agent"),
]

_BUILDER_NAME_SUFFIX: str = "_or_default"


def _exported_builder_names() -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(orchestration, inspect.isfunction)
        if name.endswith(_BUILDER_NAME_SUFFIX)
        and not name.startswith("_")
        and member.__module__ == orchestration.__name__
    }


def test_every_exported_builder_is_covered_by_a_precedence_table() -> None:
    tabled_names: set[str] = {builder.__name__ for builder, _ in _CONFIG_TAKING_BUILDERS} | {
        builder.__name__ for builder, _ in _CONFIG_FREE_BUILDERS
    }

    assert _exported_builder_names() == tabled_names


@pytest.mark.parametrize(
    ("builder", "field_name"),
    [pytest.param(builder, field_name, id=builder.__name__) for builder, field_name in _CONFIG_TAKING_BUILDERS],
)
def test_builders_with_config_with_override_return_the_override(
    builder: Callable[[PipelineOverrides, Config], object],
    field_name: str,
    fully_overridden: PipelineOverrides,
    config: Config,
) -> None:
    assert builder(fully_overridden, config) is getattr(fully_overridden, field_name)


@pytest.mark.parametrize(
    ("builder", "field_name"),
    [pytest.param(builder, field_name, id=builder.__name__) for builder, field_name in _CONFIG_FREE_BUILDERS],
)
def test_builders_without_config_with_override_return_the_override(
    builder: Callable[[PipelineOverrides], object],
    field_name: str,
    fully_overridden: PipelineOverrides,
) -> None:
    assert builder(fully_overridden) is getattr(fully_overridden, field_name)


def test_deterministic_research_tools_or_default_with_empty_overrides(
    empty_overrides: PipelineOverrides, config: Config
) -> None:
    assert isinstance(deterministic_research_tools_or_default(empty_overrides, config), DeterministicResearchTools)


def test_open_ended_research_tools_or_default_with_empty_overrides(
    empty_overrides: PipelineOverrides, config: Config
) -> None:
    assert isinstance(open_ended_research_tools_or_default(empty_overrides, config), OpenEndedResearchTools)


def test_instrument_facts_resolver_or_default_with_empty_overrides(empty_overrides: PipelineOverrides) -> None:
    """The default resolver constructs offline: the yfinance resolver is built without touching the network."""
    assert callable(instrument_facts_resolver_or_default(empty_overrides))


def test_corroboration_agent_or_default_with_empty_overrides(empty_overrides: PipelineOverrides) -> None:
    assert corroboration_agent_or_default(empty_overrides) is corroborate


_OPENAI_API_KEY_ENV: str = "OPENAI_API_KEY"
_UNUSED_OPENAI_API_KEY: str = "unused-openai-key-for-offline-construction"


@pytest.fixture
def unused_openai_api_key(monkeypatch: MonkeyPatch) -> None:
    """Pin an OpenAI key that is never spent: the LLM builders read one at construction but call nothing until run."""
    monkeypatch.setenv(_OPENAI_API_KEY_ENV, _UNUSED_OPENAI_API_KEY)


def test_thesis_agent_or_default_with_empty_overrides(
    empty_overrides: PipelineOverrides, config: Config, unused_openai_api_key: None
) -> None:
    """The default LLM thesis agent constructs offline once a key is present: nothing is spent until it is called."""
    assert callable(thesis_agent_or_default(empty_overrides, config))


def test_claim_questions_agent_or_default_with_empty_overrides(
    empty_overrides: PipelineOverrides, config: Config, unused_openai_api_key: None
) -> None:
    """The default LLM claim-questions agent constructs offline once a key is present."""
    assert callable(claim_questions_agent_or_default(empty_overrides, config))


def test_answer_synthesis_agent_or_default_with_empty_overrides(
    empty_overrides: PipelineOverrides, config: Config, unused_openai_api_key: None
) -> None:
    """The default LLM answer-synthesis agent constructs offline once a key is present."""
    assert callable(answer_synthesis_agent_or_default(empty_overrides, config))


@pytest.fixture
def overrides_with_only_open_ended_tools() -> PipelineOverrides:
    return PipelineOverrides(open_ended_tools=_OfflineOpenEndedTools())


def test_answer_synthesis_agent_or_default_routes_the_open_ended_tools_override(
    overrides_with_only_open_ended_tools: PipelineOverrides, config: Config, unused_openai_api_key: None
) -> None:
    """The composed default feeds open_ended_research_tools_or_default's result to the answer-synthesis agent.

    The constructed agent exposes its tools only as a closure variable, so the routing is read from there.
    """
    agent = answer_synthesis_agent_or_default(overrides_with_only_open_ended_tools, config)

    assert inspect.getclosurevars(agent).nonlocals["tools"] is overrides_with_only_open_ended_tools.open_ended_tools


@pytest.fixture
def without_openai_api_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv(_OPENAI_API_KEY_ENV, raising=False)


@pytest.mark.parametrize(
    "builder",
    [
        pytest.param(builder, id=builder.__name__)
        for builder in (thesis_agent_or_default, claim_questions_agent_or_default, answer_synthesis_agent_or_default)
    ],
)
def test_llm_builders_with_empty_overrides_and_no_openai_key_raise(
    builder: Callable[[PipelineOverrides, Config], object],
    empty_overrides: PipelineOverrides,
    config: Config,
    without_openai_api_key: None,
) -> None:
    """The three LLM-backed defaults fail closed at construction rather than deferring the failure into a run."""
    with pytest.raises(openai.OpenAIError):
        _ = builder(empty_overrides, config)
