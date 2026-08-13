"""Command-line interface for incremental intelligence work."""

from datetime import UTC
from datetime import datetime

import typer

from money_pit.claims.repository import ClaimNotFoundError
from money_pit.composition import execute_intelligence_update
from money_pit.composition import intelligence_work_status
from money_pit.composition import promote_canonical_claim_discovery
from money_pit.config import ConfigurationScope
from money_pit.config import load_application_config
from money_pit.constants import APP_VERSION
from money_pit.constants import DATA_ROOT
from money_pit.pipeline.orchestration import IntelligenceStage
from money_pit.reports.intelligence import intelligence_run_report
from money_pit.runs.paths import RepositoryPaths
from money_pit.storage.database import Database
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.runs import RunNotFoundError
from money_pit.storage.runs import RunRepository


intelligence_app = typer.Typer(help="Advance and inspect incremental intelligence work.")


def _database() -> Database:
    database = Database(RepositoryPaths.from_data_root(DATA_ROOT).database_path)
    database.initialize()
    return database


@intelligence_app.command(name="update")
def intelligence_update(
    source: str | None = None,
    through: IntelligenceStage = IntelligenceStage.SYNTHESIS,
) -> None:
    """Advance one bounded batch through the selected intelligence stage."""
    report = execute_intelligence_update(
        database=_database(),
        config=load_application_config(scope=ConfigurationScope.INTELLIGENCE),
        paths=RepositoryPaths.from_data_root(DATA_ROOT),
        source_id=source,
        requested_as_of=None,
        through=through,
        implementation_version=APP_VERSION,
    )
    typer.echo(report.model_dump_json(indent=2))


@intelligence_app.command(name="status")
def intelligence_status(source: str | None = None) -> None:
    """Show pending and active work without constructing providers."""
    status = intelligence_work_status(
        database=_database(),
        config=load_application_config(scope=ConfigurationScope.INTELLIGENCE),
        source_id=source,
        implementation_version=APP_VERSION,
    )
    typer.echo(status.model_dump_json(indent=2))


@intelligence_app.command(name="show")
def intelligence_show(run_id: str) -> None:
    """Show durable run progress and token usage without constructing providers."""
    database = _database()
    work = IntelligenceWorkRepository(database)
    try:
        report = intelligence_run_report(
            RunRepository(database),
            run_id=run_id,
            inference=work.usage_for_run(run_id),
        )
    except RunNotFoundError as error:
        raise typer.BadParameter(f"unknown intelligence run {run_id!r}") from error
    typer.echo(report.model_dump_json(indent=2))


@intelligence_app.command(name="promote-claim")
def intelligence_promote_claim(canonical_claim_key: str, reason: str) -> None:
    """Explicitly promote a canonical claim change into discovery work."""
    database = _database()
    try:
        unit_id = promote_canonical_claim_discovery(
            database=database,
            config=load_application_config(scope=ConfigurationScope.INTELLIGENCE),
            canonical_claim_key=canonical_claim_key,
            reason=reason,
            promoted_at=datetime.now(tz=UTC),
        )
    except (ClaimNotFoundError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(unit_id)
