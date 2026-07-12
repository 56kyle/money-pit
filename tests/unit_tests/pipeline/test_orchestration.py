"""Unit tests for orchestration's production wiring and its preserved fail-closed default.

production_deps composes the real capital-critical factories; its live seams (Alpaca credential resolution
and the spawning live_manifest) are monkeypatched at their orchestration import sites, and the Gmail app
password is served from a real in-memory keyring, so the wiring runs offline without connecting or spawning.
The second test pins that run_pipeline(overrides=None) still fails closed with MissingPipelineDependencyError.
"""

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.pipeline import orchestration
from money_pit.pipeline.orchestration import MissingPipelineDependencyError
from money_pit.pipeline.orchestration import PipelineOverrides
from money_pit.pipeline.orchestration import production_deps
from money_pit.pipeline.orchestration import run_pipeline
from tests.unit_tests.conftest import InMemoryKeyring


@pytest.fixture
def credentials() -> AlpacaCredentials:
    return AlpacaCredentials(api_key="the-key", secret_key="the-secret", paper=True)


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
    assert overrides.send_email is not None
    assert overrides.manifest is not None


def test_run_pipeline_with_no_overrides_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MissingPipelineDependencyError):
        _ = run_pipeline(signals_dir=tmp_path)
