"""Module containing source registry CLI workflows."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - Typer resolves command annotations at runtime.
from typing import TYPE_CHECKING

import typer

from money_pit.constants import ASSETS_DIRNAME
from money_pit.constants import DATA_ROOT
from money_pit.constants import STATE_DATABASE_FILENAME
from money_pit.sources.builtin import builtin_adapter_registry
from money_pit.sources.errors import SourceError
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.registry import load_source_registry
from money_pit.sources.service import EvidenceRepository
from money_pit.sources.service import SourceSyncResult
from money_pit.sources.service import SourceSyncService
from money_pit.sources.service import default_sources_path
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.errors import StorageError
from money_pit.storage.sources import SourceRepository


if TYPE_CHECKING:
    from money_pit.schemas.sources import SourceDefinition
    from money_pit.schemas.sources import SourceRegistryDocument


source_app = typer.Typer(help="Manage configured intelligence sources.")
_DEFAULT_SOURCES_PATH = default_sources_path()


def _source_runtime(
    sources_path: Path,
) -> tuple[SourceSyncService, SourceRepository]:
    adapters: AdapterRegistry = builtin_adapter_registry()
    document: SourceRegistryDocument = load_source_registry(sources_path, adapters)
    database = Database(DATA_ROOT / STATE_DATABASE_FILENAME)
    database.initialize()
    repository = SourceRepository(database)
    service = SourceSyncService(
        document,
        adapters,
        repository,
        EvidenceRepository(database),
        AssetStore(DATA_ROOT / ASSETS_DIRNAME),
    )
    _ = service.register_definitions()
    return service, repository


@source_app.command(name="list")
def list_sources(
    sources_path: Path = _DEFAULT_SOURCES_PATH,
) -> None:
    """List durable definitions after validating and registering the TOML registry."""
    try:
        _, repository = _source_runtime(sources_path)
        definitions: tuple[SourceDefinition, ...] = repository.list_definitions()
    except (SourceError, StorageError, OSError) as error:
        typer.echo(f"Cannot list sources: {error}", err=True)
        raise typer.Exit(code=1) from error
    for definition in definitions:
        state: str = "enabled" if definition.enabled else "disabled"
        typer.echo(f"{definition.source_id}\t{definition.adapter_name}\t{state}\t{definition.locator}")


@source_app.command()
def sync(
    source_id: str,
    sources_path: Path = _DEFAULT_SOURCES_PATH,
) -> None:
    """Discover, extract, and durably persist one source batch."""
    try:
        service, _ = _source_runtime(sources_path)
        result: SourceSyncResult = service.sync(source_id)
    except (SourceError, StorageError, OSError) as error:
        typer.echo(f"Cannot sync source {source_id}: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(result.model_dump_json(indent=2))


@source_app.command()
def backfill(
    source_id: str,
    maximum_batches: int = 10,
    sources_path: Path = _DEFAULT_SOURCES_PATH,
) -> None:
    """Run a bounded historical sync from an empty cursor."""
    try:
        service, _ = _source_runtime(sources_path)
        results: tuple[SourceSyncResult, ...] = service.backfill(
            source_id,
            maximum_batches=maximum_batches,
        )
    except (SourceError, StorageError, OSError) as error:
        typer.echo(f"Cannot backfill source {source_id}: {error}", err=True)
        raise typer.Exit(code=1) from error
    for result in results:
        typer.echo(result.model_dump_json())
