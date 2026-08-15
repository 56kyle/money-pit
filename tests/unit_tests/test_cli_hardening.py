import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Never

import pytest
import typer
from typer.testing import CliRunner

import money_pit.__main__ as main_cli
import money_pit.cli_support as cli_support
import money_pit.sources.cli as source_cli
from money_pit.__main__ import app
from money_pit.cli_reports import DoctorReport
from money_pit.cli_support import CliContext
from money_pit.cli_support import OperatorFailure
from money_pit.cli_support import configure_cli_context
from money_pit.cli_support import emit_progress
from money_pit.cli_support import emit_result
from money_pit.cli_support import run_operator_command
from money_pit.constants import APP_VERSION
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.sources.service import SourceIngestResult
from money_pit.storage.recovery_audit import RecoveryAuditBlockedError
from money_pit.storage.recovery_audit import RecoveryAuditDisposition
from money_pit.storage.recovery_audit import RecoveryAuditFinding
from money_pit.storage.recovery_audit import RecoveryAuditReport
from money_pit.storage.recovery_audit import RecoveryAuditStatus


class _IngestProvider:
    def ingest(self, source_id: str, url: str, *, refresh: bool = False) -> SourceIngestResult:
        del refresh
        emit_progress("provider: completed 1 item")
        return SourceIngestResult(
            source_id=source_id,
            source_item_id=url,
            ingested_count=1,
            evidence_document_count=1,
        )


def _provider_runtime(*_args: object, **_kwargs: object) -> tuple[_IngestProvider, object]:
    return _IngestProvider(), object()


def _failing_provider_runtime(*_args: object, **_kwargs: object) -> tuple[_IngestProvider, object]:
    raise RuntimeError("authorization=top-secret provider rejected request")


def _provider_failure(message: str) -> Never:
    raise RuntimeError(message)


def _write_local_sources(path: Path) -> None:
    _ = path.write_text(
        """
version = "0.0.2"
[[sources]]
source_id = "local-notes"
adapter_name = "local_text"
locator = "notes"
provenance_group = "local"
allowed_uses = ["interpretation"]
trust_settings = [{ category = "factual", level = "commentary" }]
tags = ["local"]
""".strip(),
        encoding="utf-8",
    )


def test_root_without_command_prints_help() -> None:
    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout


def test_root_version_exits_without_running_a_command() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert (result.exit_code, result.stdout.strip()) == (0, APP_VERSION)


def test_doctor_does_not_create_a_missing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_cli, "DATA_ROOT", tmp_path)

    result = CliRunner().invoke(app, ["--json", "doctor"])

    assert result.exit_code == 0
    assert not any(tmp_path.iterdir())
    report = DoctorReport.model_validate_json(result.stdout)
    storage_check = next(check for check in report.checks if check.category == "storage.schema")
    assert storage_check.detail == "database file does not exist"


@pytest.mark.parametrize("command", ["list", "status"])
def test_source_read_commands_do_not_register_or_create_storage(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources_path = tmp_path / "sources.toml"
    data_root = tmp_path / "data"
    _write_local_sources(sources_path)
    monkeypatch.setattr(source_cli, "DATA_ROOT", data_root)
    arguments = ["--json", "source", command]
    if command == "status":
        arguments.append("local-notes")
    arguments.extend(("--sources-path", str(sources_path)))

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0
    assert not data_root.exists()
    assert json.loads(result.stdout)


def test_json_command_emits_exactly_one_stdout_value_and_progress_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_cli, "_source_runtime", _provider_runtime)

    result = CliRunner().invoke(
        app,
        ["--json", "source", "ingest", "youtube", "https://example.test/item"],
    )

    values = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.exit_code == 0
    assert values == [
        {
            "evidence_document_count": 1,
            "ingested_count": 1,
            "source_id": "youtube",
            "source_item_id": "https://example.test/item",
        }
    ]
    assert "provider: completed 1 item" in result.stderr


def test_human_command_is_the_default_and_keeps_progress_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_cli, "_source_runtime", _provider_runtime)

    result = CliRunner().invoke(
        app,
        ["source", "ingest", "youtube", "https://example.test/item"],
    )

    assert result.exit_code == 0
    assert "Source ingestion" in result.stdout
    assert "source id: youtube" in result.stdout
    assert "provider: completed 1 item" not in result.stdout
    assert "provider: completed 1 item" in result.stderr


def test_operator_failure_is_sanitized_without_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_cli, "_source_runtime", _failing_provider_runtime)

    result = CliRunner().invoke(
        app,
        ["--json", "source", "ingest", "youtube", "https://example.test/item"],
    )

    failure = OperatorFailure.model_validate_json(result.stderr)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert failure.error == "authorization=<redacted>"
    assert "top-secret" not in result.stdout
    assert "top-secret" not in result.stderr
    assert failure.next_action == "Add --debug anywhere before -- to include the originating traceback."


def test_debug_adds_traceback_while_machine_failure_remains_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_cli, "_source_runtime", _failing_provider_runtime)

    result = CliRunner().invoke(
        app,
        ["--json", "--debug", "source", "ingest", "youtube", "https://example.test/item"],
    )

    failure = OperatorFailure.model_validate_json(result.stderr.splitlines()[-1])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert failure.error == "authorization=<redacted>"
    assert "Traceback (most recent call last)" in result.stderr


def test_debug_is_accepted_after_a_nested_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_cli, "_source_runtime", _failing_provider_runtime)

    result = CliRunner().invoke(
        app,
        ["source", "ingest", "youtube", "https://example.test/item", "--debug"],
    )

    assert result.exit_code == 1
    assert "Traceback (most recent call last)" in result.stderr
    assert "No such option '--debug'" not in result.stderr


@pytest.mark.parametrize(
    ("json_output", "message", "secret"),
    [
        pytest.param(True, "Authorization: Bearer top-secret", "top-secret", id="json-bearer"),
        pytest.param(False, "Authorization: Bearer top-secret", "top-secret", id="human-bearer"),
        pytest.param(True, 'password="quoted secret with spaces"', "secret with spaces", id="json-quoted"),
        pytest.param(False, 'password="quoted secret with spaces"', "secret with spaces", id="human-quoted"),
    ],
)
def test_operator_failure_redacts_complete_sensitive_values(
    json_output: bool,
    message: str,
    secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_runtime(*_args: object, **_kwargs: object) -> tuple[_IngestProvider, object]:
        return _provider_failure(message)

    monkeypatch.setattr(source_cli, "_source_runtime", fail_runtime)
    arguments = ["source", "ingest", "youtube", "https://example.test/item"]
    if json_output:
        arguments.append("--json")

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 1
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert "<redacted>" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(
            ["--json", "intelligence", "status", "--definitely-invalid"],
            id="json-before-command",
        ),
        pytest.param(
            ["intelligence", "status", "--json", "--definitely-invalid"],
            id="json-after-command",
        ),
        pytest.param(
            ["intelligence", "status", "--source", "--json"],
            id="missing-option-value",
        ),
    ],
)
def test_usage_failures_use_the_typed_json_error_contract(arguments: list[str]) -> None:
    result = CliRunner().invoke(app, arguments)

    failure = OperatorFailure.model_validate_json(result.stderr)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert failure.category == "usage"
    assert failure.failure_kind in {"BadOptionUsage", "BadParameter", "NoSuchOption", "UsageError"}
    assert failure.error


def test_root_flag_hoisting_preserves_literal_arguments_after_separator() -> None:
    command_args, separator, literal_args = cli_support._split_literal_args(  # pyright: ignore[reportPrivateUsage]
        ["source", "ingest", "youtube", "--", "--debug", "--json"]
    )

    assert command_args == ["source", "ingest", "youtube"]
    assert separator == ["--"]
    assert literal_args == ["--debug", "--json"]


def test_blocked_recovery_failure_exposes_typed_findings_and_audit_action() -> None:
    command = typer.Typer()

    @command.callback()
    def configure(context: typer.Context) -> None:  # pyright: ignore[reportUnusedFunction]
        configure_cli_context(context, CliContext(json_output=True))

    @command.command()
    def update() -> None:  # pyright: ignore[reportUnusedFunction]
        report = RecoveryAuditReport(
            status=RecoveryAuditStatus.BLOCKED,
            source_id="youtube",
            checked_at=datetime(2026, 8, 15, tzinfo=UTC),
            findings=(
                RecoveryAuditFinding(
                    code="malformed_synthesis_semantics",
                    disposition=RecoveryAuditDisposition.BLOCKED,
                    detail="Synthesis state conflicts with its evidence gate.",
                    durable_ids=("synthesis-unit:one", "synthesis-unit:two"),
                ),
            ),
        )

        def fail() -> None:
            raise RecoveryAuditBlockedError(report)

        _ = run_operator_command(fail, heading="Intelligence update")

    result = CliRunner().invoke(command, ["update"])

    failure = OperatorFailure.model_validate_json(result.stderr)
    assert result.exit_code == 1
    assert failure.failure_kind == "RecoveryAuditBlockedError"
    assert failure.diagnostics[0].code == "malformed_synthesis_semantics"
    assert failure.diagnostics[0].durable_ids == ("synthesis-unit:one", "synthesis-unit:two")
    assert failure.next_action == "Run money-pit intelligence audit --source youtube for full recovery details."


def test_blocked_recovery_failure_renders_actionable_human_details() -> None:
    command = typer.Typer()

    @command.callback()
    def configure(context: typer.Context) -> None:  # pyright: ignore[reportUnusedFunction]
        configure_cli_context(context, CliContext())

    @command.command()
    def update() -> None:  # pyright: ignore[reportUnusedFunction]
        report = RecoveryAuditReport(
            status=RecoveryAuditStatus.BLOCKED,
            source_id="youtube",
            checked_at=datetime(2026, 8, 15, tzinfo=UTC),
            findings=(
                RecoveryAuditFinding(
                    code="malformed_synthesis_semantics",
                    disposition=RecoveryAuditDisposition.BLOCKED,
                    detail="Synthesis state conflicts with its evidence gate.",
                    durable_ids=("synthesis-unit:one",),
                ),
            ),
        )

        def fail() -> None:
            raise RecoveryAuditBlockedError(report)

        _ = run_operator_command(fail, heading="Intelligence update")

    result = CliRunner().invoke(command, ["update"])

    assert result.exit_code == 1
    assert "Failure kind: RecoveryAuditBlockedError" in result.stderr
    assert "malformed_synthesis_semantics" in result.stderr
    assert "synthesis-unit:one" in result.stderr
    assert "money-pit intelligence audit --source youtube" in result.stderr


@pytest.mark.parametrize(
    ("arguments", "options"),
    [
        pytest.param(["intelligence", "update", "--help"], ("--source", "--through", "--iterations"), id="update"),
        pytest.param(["intelligence", "runs", "--help"], ("--source", "--limit"), id="runs"),
        pytest.param(
            ["intelligence", "usage", "--help"],
            ("--run-id", "--since", "--stage"),
            id="usage",
        ),
        pytest.param(["source", "list", "--help"], ("--sources-path",), id="source-list"),
        pytest.param(["source", "status", "--help"], ("--sources-path",), id="source-status"),
        pytest.param(["portfolio", "snapshot", "--help"], (), id="portfolio-snapshot"),
    ],
)
def test_cli_uses_named_options(arguments: list[str], options: tuple[str, ...]) -> None:
    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0
    assert all(option in result.stdout for option in options)


@pytest.mark.parametrize("broker_environment", ["paper", "live"])
def test_emit_result_masks_portfolio_accounts_in_human_output(broker_environment: str) -> None:
    command = typer.Typer()

    @command.callback()
    def configure(context: typer.Context) -> None:  # pyright: ignore[reportUnusedFunction]
        configure_cli_context(context, CliContext())

    @command.command()
    def snapshot() -> None:  # pyright: ignore[reportUnusedFunction]
        emit_result(
            {"account_id": "broker-account-1234", "broker_environment": broker_environment},
            heading="Portfolio snapshot",
        )

    result = CliRunner().invoke(command, ["snapshot"])

    assert result.exit_code == 0
    assert "account id: ****1234" in result.stdout
    assert "broker-account-1234" not in result.stdout
    assert f"broker environment: {broker_environment}" in result.stdout


def test_emit_result_preserves_portfolio_account_in_json_output() -> None:
    command = typer.Typer()

    @command.callback()
    def configure(context: typer.Context) -> None:  # pyright: ignore[reportUnusedFunction]
        configure_cli_context(context, CliContext(json_output=True))

    @command.command()
    def snapshot() -> None:  # pyright: ignore[reportUnusedFunction]
        emit_result(
            {"account_id": "broker-account-1234", "broker_environment": "paper"},
            heading="Portfolio snapshot",
        )

    result = CliRunner().invoke(command, ["snapshot"])

    assert json.loads(result.stdout) == {
        "account_id": "broker-account-1234",
        "broker_environment": "paper",
    }


def test_real_portfolio_snapshot_human_output_prominently_masks_account() -> None:
    snapshot = PortfolioStateSnapshot.from_payload(
        PortfolioStatePayload(
            account_id="broker-account-1234",
            broker_environment=BrokerEnvironment.PAPER,
            captured_at=datetime(2026, 8, 13, tzinfo=UTC),
            available_cash=100.0,
            positions=(),
            open_order_ids=(),
        )
    )

    rendered = main_cli._portfolio_human_value(snapshot)  # pyright: ignore[reportPrivateUsage]

    assert isinstance(rendered, dict)
    assert rendered["environment"] == "PAPER"
    assert rendered["payload"]["account_id"] == "****1234"
