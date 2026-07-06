"""Test cases for the __main__ module — the money-pit no-op command and the pin-order-schema introspector.

pin-order-schema's live seams (credential resolution, MCP introspection) are monkeypatched at their
__main__ import sites, and the committed schema path is redirected to a tmp file so a test never overwrites
the real alpaca_order_schema.json. Assertions target CLI exit codes and the written file's shape.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

from money_pit import __main__
from money_pit.config import AlpacaCredentials
from money_pit.config import CredentialResolutionError
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL


@dataclass
class _FakeTool:
    """A minimal stand-in for mcp.types.Tool carrying the fields pin_order_schema reads."""

    name: str
    inputSchema: dict[str, object]  # noqa: N815 - mirrors the mcp.types.Tool attribute name


@pytest.fixture
def runner() -> CliRunner:
    """Fixture for invoking command-line interfaces."""
    return CliRunner()


@pytest.fixture
def credentials() -> AlpacaCredentials:
    return AlpacaCredentials(api_key="the-key", secret_key="the-secret", paper=True)


@pytest.fixture
def stub_credential_resolution(monkeypatch: MonkeyPatch, credentials: AlpacaCredentials) -> None:
    monkeypatch.setattr(__main__, "load_config", lambda: None)
    monkeypatch.setattr(__main__, "resolve_alpaca_credentials", lambda _config: credentials)


def test_main_succeeds(runner: CliRunner) -> None:
    """It exits with a status code of zero."""
    result = runner.invoke(__main__.app, ["money-pit"])
    assert result.exit_code == 0


def test_pin_order_schema_with_credential_failure(runner: CliRunner, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(__main__, "load_config", lambda: None)

    def _raise(_config: object) -> AlpacaCredentials:
        raise CredentialResolutionError("no secret")

    monkeypatch.setattr(__main__, "resolve_alpaca_credentials", _raise)

    result = runner.invoke(__main__.app, ["pin-order-schema"])

    assert result.exit_code == 1


def test_pin_order_schema_with_introspection_failure(
    runner: CliRunner, monkeypatch: MonkeyPatch, stub_credential_resolution: None
) -> None:
    def _raise(_credentials: AlpacaCredentials) -> list[_FakeTool]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(__main__, "list_write_tools", _raise)

    result = runner.invoke(__main__.app, ["pin-order-schema"])

    assert result.exit_code == 1


def test_pin_order_schema_with_tool_absent(
    runner: CliRunner, monkeypatch: MonkeyPatch, stub_credential_resolution: None
) -> None:
    monkeypatch.setattr(__main__, "list_write_tools", lambda _credentials: [_FakeTool("place_option_order", {})])

    result = runner.invoke(__main__.app, ["pin-order-schema"])

    assert result.exit_code == 1


def test_pin_order_schema_with_success_strips_sentinel(
    runner: CliRunner, monkeypatch: MonkeyPatch, stub_credential_resolution: None, tmp_path: Path
) -> None:
    schema: dict[str, object] = {"type": "object", ALPACA_ORDER_SCHEMA_STUB_SENTINEL: True}
    monkeypatch.setattr(
        __main__, "list_write_tools", lambda _credentials: [_FakeTool("place_stock_order", schema)]
    )
    schema_path: Path = tmp_path / "alpaca_order_schema.json"
    monkeypatch.setattr(__main__, "ALPACA_ORDER_SCHEMA_PATH", schema_path)

    result = runner.invoke(__main__.app, ["pin-order-schema"])

    assert result.exit_code == 0
    written: dict[str, object] = json.loads(schema_path.read_text(encoding="utf-8"))
    assert ALPACA_ORDER_SCHEMA_STUB_SENTINEL not in written
    assert written["type"] == "object"
