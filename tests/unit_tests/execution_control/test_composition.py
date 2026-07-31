"""Tests for fail-closed production dependency composition."""

import pytest
from pytest import MonkeyPatch

from money_pit.config import Config
from money_pit.pipeline import orchestration
from money_pit.schemas.execution_policy import ExecutionMode


def _unused(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.mark.parametrize("mode", list(ExecutionMode))
def test_production_deps_never_constructs_uncovered_writer(
    monkeypatch: MonkeyPatch,
    mode: ExecutionMode,
) -> None:
    config = Config(
        alpaca_service="service",
        alpaca_username="user",
        alpaca_paper=True,
        execution_mode=mode,
    )
    credential_sentinel = object()
    monkeypatch.setattr(orchestration, "resolve_alpaca_credentials", lambda _config: credential_sentinel)
    monkeypatch.setattr(orchestration, "portfolio_fetcher_or_default", lambda _overrides, _config: _unused)
    monkeypatch.setattr(orchestration, "make_alpaca_fill_observer", lambda _credentials: _unused)
    monkeypatch.setattr(orchestration, "_gmail_or_unconfigured_email_sender", lambda _config: _unused)

    dependencies = orchestration.production_deps(config)

    assert (dependencies.place_order, dependencies.manifest) == (None, None)
