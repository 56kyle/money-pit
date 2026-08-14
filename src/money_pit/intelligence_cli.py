"""Command-line interface for incremental intelligence work."""

# Typer's Option overloads expose partially typed Click internals.
# pyright: reportUnknownMemberType=false

from datetime import UTC
from datetime import datetime
from typing import Annotated

import typer

from money_pit.agents.inference import InferenceUsage
from money_pit.cli_reports import IntelligenceStatusReport
from money_pit.cli_reports import IntelligenceUpdateBatchReport
from money_pit.cli_reports import IntelligenceUsageReport
from money_pit.cli_support import emit_progress
from money_pit.cli_support import run_operator_command
from money_pit.composition import execute_intelligence_update
from money_pit.composition import intelligence_work_status
from money_pit.composition import promote_canonical_claim_discovery
from money_pit.config import ConfigurationScope
from money_pit.config import load_application_config
from money_pit.constants import APP_VERSION
from money_pit.constants import DATA_ROOT
from money_pit.pipeline.orchestration import IntelligenceStage
from money_pit.pipeline.orchestration import IntelligenceUpdateReport
from money_pit.reports.intelligence import intelligence_run_report
from money_pit.runs.paths import RepositoryPaths
from money_pit.storage.database import Database
from money_pit.storage.database import DatabaseSchemaState
from money_pit.storage.database import inspect_database
from money_pit.storage.intelligence_work import IntelligenceWorkCompletionCounts
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.runs import RunRepository


intelligence_app = typer.Typer(help="Advance and inspect incremental intelligence work.")
_UNUSUALLY_LARGE_INPUT_TOKENS_PER_UNIT = 100_000


def _database() -> Database:
    database = Database(RepositoryPaths.from_data_root(DATA_ROOT).database_path)
    database.initialize()
    return database


def _read_database() -> Database:
    """Open only an existing current database; never create or migrate it."""
    path = RepositoryPaths.from_data_root(DATA_ROOT).database_path
    inspection = inspect_database(path)
    if inspection.state is not DatabaseSchemaState.CURRENT:
        raise RuntimeError(
            f"Database is {inspection.state.value}; run an updating command to initialize or migrate it."
        )
    return Database(path)


@intelligence_app.command(name="update")
def intelligence_update(
    source: str | None = None,
    through: IntelligenceStage = IntelligenceStage.SYNTHESIS,
    iterations: Annotated[int, typer.Option(min=1, help="Maximum number of separately durable runs.")] = 1,
) -> None:
    """Advance bounded work in separate durable runs up to the iteration limit."""
    _ = run_operator_command(
        lambda: _run_intelligence_updates(source=source, through=through, iterations=iterations),
        heading="Intelligence update",
    )


def _run_intelligence_updates(
    *,
    source: str | None,
    through: IntelligenceStage,
    iterations: int,
) -> IntelligenceUpdateBatchReport:
    database = _database()
    config = load_application_config(scope=ConfigurationScope.INTELLIGENCE)
    paths = RepositoryPaths.from_data_root(DATA_ROOT)
    reports: list[IntelligenceUpdateReport] = []
    stopped = False
    for iteration in range(1, iterations + 1):
        emit_progress(f"intelligence update: starting durable run {iteration} of {iterations}")
        report = execute_intelligence_update(
            database=database,
            config=config,
            paths=paths,
            source_id=source,
            requested_as_of=None,
            through=through,
            implementation_version=APP_VERSION,
        )
        reports.append(report)
        transition_count = getattr(report, "durable_transition_count", None)
        if transition_count is None:
            raise RuntimeError("Intelligence update report does not include a durable transition count.")
        emit_progress(f"intelligence update: committed {transition_count} durable transitions")
        if transition_count == 0:
            stopped = True
            break
    return IntelligenceUpdateBatchReport(
        requested_iterations=iterations,
        performed_iterations=len(reports),
        stopped_because_no_transition=stopped,
        run_ids=tuple(report.run_id for report in reports),
        durable_transition_count=sum(report.durable_transition_count for report in reports),
        completed=IntelligenceWorkCompletionCounts(
            interpretation_bundles=sum(report.completed.interpretation_bundles for report in reports),
            interpretation_chunks=sum(report.completed.interpretation_chunks for report in reports),
            discovery_units=sum(report.completed.discovery_units for report in reports),
            research_jobs=sum(report.completed.research_jobs for report in reports),
            synthesis_units=sum(report.completed.synthesis_units for report in reports),
        ),
        usage=sum((report.usage.usage for report in reports), start=InferenceUsage()),
        failed_call_count=sum(report.usage.failed_call_count for report in reports),
        unavailable_usage_call_count=sum(report.usage.unavailable_usage_call_count for report in reports),
        remaining=reports[-1].remaining,
        runs=tuple(reports),
    )


@intelligence_app.command(name="status")
def intelligence_status(source: str | None = None) -> None:
    """Show pending and active work without constructing providers."""
    _ = run_operator_command(
        lambda: _intelligence_status_report(source),
        heading="Intelligence status",
    )


def _intelligence_status_report(source: str | None) -> IntelligenceStatusReport:
    status = intelligence_work_status(
        database=_read_database(),
        config=load_application_config(scope=ConfigurationScope.INTELLIGENCE),
        source_id=source,
        implementation_version=APP_VERSION,
    )
    categories = {
        "pending": sum(
            (
                status.pending_interpretation_bundles,
                status.pending_interpretation_chunks,
                status.pending_discovery_units,
                status.pending_research_jobs,
                status.pending_synthesis_units,
            )
        ),
        "active": sum(
            (
                status.active_interpretation_bundles,
                status.active_interpretation_chunks,
                status.active_discovery_units,
                status.active_research_jobs,
                status.active_synthesis_units,
            )
        ),
        "due_reviews": status.due_research_reviews,
        "unmaterialized": (status.unmaterialized_interpretation_bundles + status.unmaterialized_discovery_units),
        "unavailable": "not checked (provider-free status)",
    }
    if categories["active"]:
        next_action = "Inspect the most recent run, then retry the matching intelligence update."
    elif categories["pending"] or categories["due_reviews"] or categories["unmaterialized"]:
        next_action = "Run intelligence update to advance the queued durable work."
    else:
        next_action = "No intelligence update is required."
    return IntelligenceStatusReport(
        source_id=source,
        next_action=next_action,
        categories=categories,
        durable=status,
    )


@intelligence_app.command(name="runs")
def intelligence_runs(
    source: str | None = None,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 20,
) -> None:
    """List recent durable intelligence runs without constructing providers."""
    _ = run_operator_command(
        lambda: RunRepository(_read_database()).recent_intelligence_runs(limit=limit, source_id=source),
        heading="Intelligence runs",
    )


@intelligence_app.command(name="usage")
def intelligence_usage(
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    since: Annotated[datetime | None, typer.Option("--since")] = None,
    stage: Annotated[str | None, typer.Option("--stage")] = None,
) -> None:
    """Show provider-reported token usage for an optional durable filter."""
    _ = run_operator_command(
        lambda: _usage_report(run_id=run_id, since=since, stage=stage),
        heading="Intelligence usage",
    )


def _usage_report(*, run_id: str | None, since: datetime | None, stage: str | None) -> IntelligenceUsageReport:
    usage = IntelligenceWorkRepository(_read_database()).filtered_usage(run_id=run_id, since=since, stage=stage)
    diagnostics: list[str] = []
    if (
        usage.input_tokens_per_completed_unit is not None
        and usage.input_tokens_per_completed_unit >= _UNUSUALLY_LARGE_INPUT_TOKENS_PER_UNIT
    ):
        diagnostics.append("Unusually large input-token use per completed work unit.")
    if usage.usage.input_tokens > 0 and usage.completed_unit_count == 0:
        diagnostics.append("Inference tokens were used without a completed work unit in this selection.")
    if usage.failed_call_count:
        diagnostics.append("Failed inference calls consumed provider requests; inspect the stage breakdown.")
    if usage.unavailable_usage_call_count:
        diagnostics.append(
            "Some calls lack provider usage; totals exclude unknown tokens rather than treating them as zero."
        )
    return IntelligenceUsageReport(usage=usage, diagnostics=tuple(diagnostics))


@intelligence_app.command(name="show")
def intelligence_show(run_id: str) -> None:
    """Show durable run progress and token usage without constructing providers."""

    def run() -> object:
        database = _read_database()
        work = IntelligenceWorkRepository(database)
        return intelligence_run_report(
            RunRepository(database),
            run_id=run_id,
            inference=work.usage_for_run(run_id),
        )

    _ = run_operator_command(run, heading="Intelligence run")


@intelligence_app.command(name="promote-claim")
def intelligence_promote_claim(
    canonical_claim_key: str,
    reason: Annotated[str, typer.Option("--reason")],
) -> None:
    """Explicitly promote a canonical claim change into discovery work."""

    def run() -> dict[str, str]:
        database = _database()
        unit_id = promote_canonical_claim_discovery(
            database=database,
            config=load_application_config(scope=ConfigurationScope.INTELLIGENCE),
            canonical_claim_key=canonical_claim_key,
            reason=reason,
            promoted_at=datetime.now(tz=UTC),
        )
        return {"discovery_unit_id": unit_id}

    _ = run_operator_command(run, heading="Promoted claim")
