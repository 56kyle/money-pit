from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from money_pit import intelligence_cli
from money_pit.__main__ import app
from money_pit.config import ApplicationConfig
from money_pit.config import ConfigurationScope
from money_pit.config import load_application_config
from money_pit.intelligence_cli import intelligence_app
from money_pit.schemas.runs import RunRecord
from money_pit.storage.database import Database
from money_pit.storage.runs import RunRepository


_NOW = datetime(2026, 8, 12, 20, tzinfo=UTC)


def _database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def _reject_configuration_load(
    *,
    sources_path: Path | None = None,
    strategy_path: Path | None = None,
    execution_path: Path | None = None,
    scope: ConfigurationScope = ConfigurationScope.EXECUTION,
) -> ApplicationConfig:
    raise AssertionError((sources_path, strategy_path, execution_path, scope))


def test_intelligence_status_loads_only_nonsecret_intelligence_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path / "status.sqlite3")
    config = load_application_config(scope=ConfigurationScope.INTELLIGENCE)
    observed_scopes: list[ConfigurationScope] = []

    def load_intelligence_config(**kwargs: object) -> ApplicationConfig:
        scope = kwargs.get("scope")
        assert isinstance(scope, ConfigurationScope)
        observed_scopes.append(scope)
        return config

    monkeypatch.setattr(intelligence_cli, "_database", lambda: database)
    monkeypatch.setattr(
        intelligence_cli,
        "load_application_config",
        load_intelligence_config,
    )

    result = CliRunner().invoke(intelligence_app, ["status"])

    assert result.exit_code == 0
    assert observed_scopes == [ConfigurationScope.INTELLIGENCE]


def test_intelligence_show_reports_durable_usage_without_loading_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path / "show.sqlite3")
    run_id = "4fa85f64-5717-4562-b3fc-2c963f66afa6"
    RunRepository(database).append_run(
        RunRecord(
            run_id=run_id,
            requested_as_of=_NOW,
            started_at=_NOW,
            known_at=_NOW,
            through_stage="A4",
            source_config_hash="a" * 64,
            intelligence_config_hash="b" * 64,
        )
    )
    monkeypatch.setattr(intelligence_cli, "_database", lambda: database)
    monkeypatch.setattr(
        intelligence_cli,
        "load_application_config",
        _reject_configuration_load,
    )

    result = CliRunner().invoke(intelligence_app, ["show", run_id])

    assert result.exit_code == 0
    assert f'"run_id": "{run_id}"' in result.stdout
    assert '"logical_call_count": 0' in result.stdout


def test_root_cli_has_no_ambiguous_run_command() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "\n run " not in result.stdout.lower()
