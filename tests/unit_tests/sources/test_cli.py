from collections.abc import Callable
from pathlib import Path

import pytest
import typer
from pytest import CaptureFixture
from pytest import MonkeyPatch

from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceDefinition
from money_pit.sources import cli
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.service import SourceSyncResult


class _SourceRepository:
    def list_definitions(self) -> tuple[SourceDefinition, ...]:
        return (
            SourceDefinition(source_id="enabled", adapter_name="manual", locator="text"),
            SourceDefinition(
                source_id="disabled",
                adapter_name="manual",
                enabled=False,
                locator="text",
            ),
        )


class _SourceService:
    def sync(self, source_id: str) -> SourceSyncResult:
        return SourceSyncResult(
            source_id=source_id,
            discovered_count=1,
            persisted_count=1,
            evidence_document_count=1,
            next_cursor=SourceCursor(value="next"),
        )

    def backfill(
        self,
        source_id: str,
        *,
        maximum_batches: int,
    ) -> tuple[SourceSyncResult, ...]:
        return tuple(
            SourceSyncResult(
                source_id=source_id,
                discovered_count=1,
                persisted_count=1,
                evidence_document_count=1,
                next_cursor=None,
            )
            for _ in range(maximum_batches)
        )


def _runtime(_sources_path: Path) -> tuple[_SourceService, _SourceRepository]:
    return _SourceService(), _SourceRepository()


def test_list_sources_prints_enabled_and_disabled_states(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_source_runtime", _runtime)

    cli.list_sources(Path("sources.toml"))

    assert capsys.readouterr().out.splitlines() == [
        "enabled\tmanual\tenabled\ttext",
        "disabled\tmanual\tdisabled\ttext",
    ]


def test_sync_prints_result(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_source_runtime", _runtime)

    cli.sync("source", Path("sources.toml"))

    assert '"source_id": "source"' in capsys.readouterr().out


def test_backfill_prints_each_result(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_source_runtime", _runtime)

    cli.backfill("source", 2, Path("sources.toml"))

    assert capsys.readouterr().out.count('"source_id":"source"') == 2


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            lambda: cli.list_sources(Path("sources.toml")),
            "Cannot list sources: unavailable",
        ),
        (
            lambda: cli.sync("source", Path("sources.toml")),
            "Cannot sync source source: unavailable",
        ),
        (
            lambda: cli.backfill("source", 2, Path("sources.toml")),
            "Cannot backfill source source: unavailable",
        ),
    ],
)
def test_source_commands_wrap_source_failures(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    command: Callable[[], None],
    expected: str,
) -> None:
    def fail(_sources_path: Path) -> tuple[_SourceService, _SourceRepository]:
        raise SourceDiscoveryError("unavailable")

    monkeypatch.setattr(cli, "_source_runtime", fail)

    with pytest.raises(typer.Exit) as raised:
        command()

    assert (raised.value.exit_code, capsys.readouterr().err.strip()) == (1, expected)
