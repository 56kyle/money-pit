"""Module containing source registry CLI workflows."""

# Typer's Option overloads expose partially typed Click internals.
# pyright: reportUnknownMemberType=false

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path  # noqa: TC003 - Typer resolves command annotations at runtime.
from typing import TYPE_CHECKING
from typing import Annotated
from typing import cast

import typer

from money_pit.cli_reports import SourceStatusReport
from money_pit.cli_support import emit_progress
from money_pit.cli_support import run_operator_command
from money_pit.config import ConfigurationScope
from money_pit.config import load_application_config
from money_pit.constants import APP_VERSION
from money_pit.constants import ASSETS_DIRNAME
from money_pit.constants import DATA_ROOT
from money_pit.constants import STATE_DATABASE_FILENAME
from money_pit.evidence.processors import builtin_evidence_processors
from money_pit.evidence.repository import EvidenceProcessingAttemptRepository
from money_pit.evidence.work import EvidenceWorkStore
from money_pit.pipeline.identity import interpretation_policy_version
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.secrets import SecretSpecInferenceResolver
from money_pit.secrets import SecretSpecSourceResolver
from money_pit.sources.builtin import builtin_adapter_registry
from money_pit.sources.errors import SourceError
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.registry import load_source_registry
from money_pit.sources.service import EvidenceRepository
from money_pit.sources.service import SourceIngestResult
from money_pit.sources.service import SourceSyncResult
from money_pit.sources.service import SourceSyncService
from money_pit.sources.service import default_sources_path
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.sources import SourceRepository


if TYPE_CHECKING:
    import sqlite3

    from money_pit.progress import IngestionProgressCallback
    from money_pit.progress import IngestionProgressEvent
    from money_pit.schemas.sources import SourceRegistryDocument


source_app = typer.Typer(help="Manage configured intelligence sources.")
_DEFAULT_SOURCES_PATH = default_sources_path()


def _configured_sources(sources_path: Path) -> tuple[SourceDefinition, ...]:
    """Parse and validate source definitions without credentials or storage."""
    configuration = load_application_config(
        sources_path=sources_path,
        scope=ConfigurationScope.INTELLIGENCE,
    )
    adapters = builtin_adapter_registry(None, configuration.intelligence)
    document = load_source_registry(sources_path, adapters)
    return document.sources


def _source_runtime(
    sources_path: Path,
    *,
    progress: IngestionProgressCallback | None = None,
) -> tuple[SourceSyncService, SourceRepository]:
    configuration = load_application_config(
        sources_path=sources_path,
        scope=ConfigurationScope.INTELLIGENCE,
    )
    source_credentials = SecretSpecSourceResolver.from_environment()
    inference_credentials = SecretSpecInferenceResolver.from_environment()
    adapters: AdapterRegistry = builtin_adapter_registry(
        source_credentials,
        configuration.intelligence,
        inference_credentials=inference_credentials,
        progress=progress,
    )
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
        attempt_repository=EvidenceProcessingAttemptRepository(database),
        processor_registry=builtin_evidence_processors(
            inference_credentials,
            model=configuration.intelligence.llm_model,
            progress=progress,
        ),
        **({"progress": progress} if progress is not None else {}),
    )
    _ = service.register_definitions()
    return service, repository


@source_app.command(name="list")
def list_sources(
    sources_path: Path = _DEFAULT_SOURCES_PATH,
) -> None:
    """List validated configured definitions without changing durable state."""
    _ = run_operator_command(
        lambda: _configured_sources(sources_path),
        heading="Configured sources",
    )


@source_app.command(name="check")
def check_sources(sources_path: Path = _DEFAULT_SOURCES_PATH) -> None:
    """Validate the source registry without credentials, providers, or storage."""
    _ = run_operator_command(
        lambda: {
            "valid": True,
            "source_count": len(_configured_sources(sources_path)),
            "path": str(sources_path),
        },
        heading="Source configuration check",
    )


@source_app.command(name="status")
def source_status(source_id: str, sources_path: Path = _DEFAULT_SOURCES_PATH) -> None:
    """Show configured state and durable cursors without changing storage."""
    _ = run_operator_command(
        lambda: _source_status(source_id, sources_path),
        heading="Source status",
    )


def _source_status(source_id: str, sources_path: Path) -> SourceStatusReport:
    configuration = load_application_config(sources_path=sources_path, scope=ConfigurationScope.INTELLIGENCE)
    definitions: tuple[SourceDefinition, ...] = _configured_sources(sources_path)
    definition: SourceDefinition | None = next(
        (item for item in definitions if item.source_id == source_id),
        None,
    )
    if definition is None:
        raise SourceError(f"Source {source_id!r} is not configured.")
    database_path: Path = DATA_ROOT / STATE_DATABASE_FILENAME
    if not database_path.is_file():
        return SourceStatusReport(
            source_id=source_id,
            configured=True,
            enabled=definition.enabled,
            sync_cursor_present=False,
            backfill_cursor_present=False,
        )
    repository = SourceRepository(Database(database_path))
    cursors = repository.cursor_statuses(source_id)
    sync_status = next((item for item in cursors if item.purpose is SourceCursorPurpose.SYNC), None)
    backfill_status = next((item for item in cursors if item.purpose is SourceCursorPurpose.BACKFILL), None)
    database = Database(database_path)
    with database.read_only_transaction() as connection:
        row = cast(
            "sqlite3.Row | None",
            connection.execute(
                """SELECT max(acquisition.retrieved_at) FROM evidence_asset_acquisitions AS acquisition
                JOIN source_items AS item ON item.source_item_id = acquisition.source_item_id
                WHERE item.source_id = ?""",
                (source_id,),
            ).fetchone(),
        )
    stored_activity: object | None = None if row is None else cast("object | None", row[0])
    last_activity = None if stored_activity is None else datetime.fromisoformat(str(stored_activity))
    work_status = IntelligenceWorkRepository(database).status(source_id)
    pending_work = sum(
        (
            work_status.pending_interpretation_bundles,
            work_status.pending_interpretation_chunks,
            work_status.pending_discovery_units,
            work_status.pending_research_jobs,
            work_status.pending_synthesis_units,
            work_status.due_research_reviews,
        )
    )
    interpreter_version = interpretation_policy_version(
        prompt_version=APP_VERSION,
        model=configuration.intelligence.llm_model,
    )
    pending_documents = EvidenceWorkStore(database).list_pending_documents(
        as_of=datetime.now(tz=UTC),
        source_id=source_id,
        limit=2_147_483_647,
        interpreter_version=interpreter_version,
    )
    unmaterialized = {
        (item.document.asset.source_item_id, item.content_version)
        for item in pending_documents
        if not IntelligenceWorkRepository(database).has_interpretation_bundle(
            source_item_id=item.document.asset.source_item_id,
            content_version=item.content_version,
            interpreter_version=interpreter_version,
        )
    }
    return SourceStatusReport(
        source_id=source_id,
        configured=True,
        enabled=definition.enabled,
        sync_cursor_present=sync_status is not None,
        backfill_cursor_present=backfill_status is not None,
        sync_cursor_updated_at=None if sync_status is None else sync_status.updated_at,
        backfill_cursor_updated_at=None if backfill_status is None else backfill_status.updated_at,
        last_activity_at=last_activity,
        pending_work_count=pending_work + len(unmaterialized),
    )


@source_app.command()
def sync(
    source_id: str,
    sources_path: Path = _DEFAULT_SOURCES_PATH,
) -> None:
    """Discover, extract, and durably persist one source batch."""

    def run() -> SourceSyncResult:
        service, _ = _source_runtime(sources_path, progress=_render_ingestion_progress)
        emit_progress(f"sync: starting {source_id}")
        result: SourceSyncResult = service.sync(source_id)
        emit_progress(f"sync: committed {source_id}")
        return result

    _ = run_operator_command(run, heading="Source sync")


@source_app.command()
def backfill(
    source_id: str,
    maximum_batches: int = 10,
    sources_path: Path = _DEFAULT_SOURCES_PATH,
) -> None:
    """Run a bounded historical sync from an empty cursor."""

    def run() -> tuple[SourceSyncResult, ...]:
        service, _ = _source_runtime(sources_path, progress=_render_ingestion_progress)
        emit_progress(f"backfill: starting {source_id}")
        results: tuple[SourceSyncResult, ...] = service.backfill(
            source_id,
            maximum_batches=maximum_batches,
        )
        emit_progress(f"backfill: committed {len(results)} batches")
        return results

    _ = run_operator_command(run, heading="Source backfill")


@source_app.command()
def ingest(
    source_id: str,
    url: str,
    sources_path: Path = _DEFAULT_SOURCES_PATH,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Download again instead of reusing cached media.",
        ),
    ] = False,
) -> None:
    """Ingest one caller-selected URL through its configured source policy."""

    def run() -> SourceIngestResult:
        service, _ = _source_runtime(sources_path, progress=_render_ingestion_progress)
        return service.ingest(source_id, url, refresh=refresh)

    _ = run_operator_command(run, heading="Source ingestion")


def _render_ingestion_progress(event: IngestionProgressEvent) -> None:
    """Render one ingestion update to stderr without contaminating JSON stdout."""
    parts: list[str] = [event.stage.value.replace("_", " ")]
    if event.detail is not None:
        parts.append(event.detail)
    if event.current is not None and event.total is not None:
        percentage: float = min(event.current / event.total * 100.0, 100.0)
        parts.append(f"{percentage:.1f}%")
    elif event.current is not None:
        parts.append(f"{event.current:g}")
    emit_progress(": ".join(parts))
