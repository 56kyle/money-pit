"""Command-line interface for persistent money_pit intelligence."""

# Typer's Option overloads expose partially typed Click internals.
# pyright: reportUnknownMemberType=false

import os
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Annotated
from typing import cast

import typer
from pydantic import BaseModel

from money_pit.claims.cli import claims_app
from money_pit.cli_reports import DoctorCheck
from money_pit.cli_reports import DoctorReport
from money_pit.cli_reports import ReplayReport
from money_pit.cli_support import CliContext
from money_pit.cli_support import configure_cli_context
from money_pit.cli_support import run_operator_command
from money_pit.composition import execute_portfolio_review
from money_pit.config import ApplicationConfig
from money_pit.config import ConfigurationError
from money_pit.config import ConfigurationScope
from money_pit.config import canonical_config_hash
from money_pit.config import load_application_config
from money_pit.constants import APP_VERSION
from money_pit.constants import DATA_ROOT
from money_pit.constants import default_config_path
from money_pit.constants import default_execution_config_path
from money_pit.constants import default_sources_config_path
from money_pit.constants import default_strategy_config_path
from money_pit.execution_control.gateway import execute_plan
from money_pit.execution_control.models import ApprovalDecision
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.execution_control.service import ApprovalBinding
from money_pit.execution_control.service import disable_execution
from money_pit.execution_control.service import enable_execution
from money_pit.execution_control.service import record_plan_decision
from money_pit.intelligence_cli import intelligence_app
from money_pit.pipeline.replay import make_replay_node
from money_pit.plans.repository import PortfolioPlanRepository
from money_pit.portfolio.composition import PortfolioRuntime
from money_pit.portfolio.composition import build_portfolio_runtime
from money_pit.portfolio.repository import SnapshotRepository
from money_pit.portfolio.theses import ThesisRepository
from money_pit.research.repository import ResearchRepository
from money_pit.runs.paths import RepositoryPaths
from money_pit.runs.repository import RunRepository
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.research import CandidateThesisResearchScope
from money_pit.schemas.research import CanonicalClaimResearchScope
from money_pit.secrets import DEFAULT_SECRETSPEC_PROFILE
from money_pit.secrets import SECRETSPEC_PROFILE_ENV
from money_pit.secrets import SecretSpecExecutionResolver
from money_pit.secrets import SecretSpecPortfolioResolver
from money_pit.sources.cli import source_app
from money_pit.storage.database import Database
from money_pit.storage.database import DatabaseSchemaState
from money_pit.storage.database import inspect_database
from money_pit.storage.runs import RunRepository as DurableRunRepository


app = typer.Typer(help="Persistent point-in-time investment research.", invoke_without_command=True)
research_app = typer.Typer(help="Inspect bounded durable research.")
theses_app = typer.Typer(help="Inspect persistent thesis history.")
portfolio_app = typer.Typer(help="Capture and review portfolio decisions.")
plan_app = typer.Typer(help="Inspect, decide, and execute exact-hash plans.")
execution_app = typer.Typer(help="Manage global execution authority.")
app.add_typer(source_app, name="source")
app.add_typer(intelligence_app, name="intelligence")
app.add_typer(research_app, name="research")
app.add_typer(claims_app, name="claims")
app.add_typer(theses_app, name="theses")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(plan_app, name="plan")
app.add_typer(execution_app, name="execution")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(APP_VERSION)
        raise typer.Exit()


@app.callback()
def main(
    context: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Write exactly one JSON value to stdout.")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Write a traceback to stderr on failure.")] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the version and exit."),
    ] = False,
) -> None:
    """Configure output for this command invocation."""
    del version
    configure_cli_context(context, CliContext(json_output=json_output, debug=debug))
    if context.invoked_subcommand is None:
        typer.echo(context.get_help())


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


def _portfolio_runtime(
    database: Database,
    *,
    scope: ConfigurationScope = ConfigurationScope.EXECUTION,
) -> PortfolioRuntime:
    config = load_application_config(scope=scope)
    portfolio_credentials = SecretSpecPortfolioResolver.from_environment()
    paths = RepositoryPaths.from_data_root(DATA_ROOT)
    return build_portfolio_runtime(
        database=database,
        config=config,
        reports_root=paths.reports_root,
        processor_versions={"builtin_evidence_processors": APP_VERSION},
        model_versions={"llm": config.intelligence.llm_model},
        prompt_versions={"portfolio": APP_VERSION},
        implementation_version=APP_VERSION,
        portfolio_credentials=portfolio_credentials,
        execution_credentials=(
            SecretSpecExecutionResolver.from_environment() if scope is ConfigurationScope.EXECUTION else None
        ),
    )


@research_app.command(name="list")
def research_list() -> None:
    """List durable research sessions."""

    def run() -> tuple[dict[str, str | None], ...]:
        rows: list[dict[str, str | None]] = []
        for session in ResearchRepository(_database()).sessions():
            scope = session.scope
            if isinstance(scope, CandidateThesisResearchScope):
                subject = scope.candidate_thesis_id
            elif isinstance(scope, CanonicalClaimResearchScope):
                subject = scope.canonical_claim_key
            else:
                subject = scope.observation_id
            rows.append(
                {
                    "session_id": session.session_id,
                    "scope": scope.kind,
                    "subject": subject,
                    "status": session.status.value,
                    "stop_reason": None if session.stop_reason is None else session.stop_reason.value,
                }
            )
        return tuple(rows)

    _ = run_operator_command(run, heading="Research sessions")


@research_app.command(name="show")
def research_show(session_id: str) -> None:
    """Show one durable research session."""

    def run() -> object:
        session = ResearchRepository(_database()).session(session_id)
        if session is None:
            raise typer.BadParameter(f"unknown research session {session_id!r}")
        return session

    _ = run_operator_command(run, heading="Research session")


@theses_app.command(name="list")
def theses_list(as_of: datetime | None = None) -> None:
    """List current point-in-time thesis revisions."""
    cutoff = as_of or datetime.now(tz=UTC)
    _ = run_operator_command(
        lambda: ThesisRepository(_database()).revisions_as_of(as_of=cutoff),
        heading="Theses",
    )


@theses_app.command(name="show")
def theses_show(revision_id: str) -> None:
    """Show one immutable thesis revision."""

    def run() -> object:
        revision = ThesisRepository(_database()).get_revision(revision_id)
        if revision is None:
            raise typer.BadParameter(f"unknown thesis revision {revision_id!r}")
        return revision

    _ = run_operator_command(run, heading="Thesis")


@portfolio_app.command(name="snapshot")
def portfolio_snapshot() -> None:
    """Capture and persist the current read-only broker portfolio state."""

    def run() -> object:
        database = _database()
        snapshot = _portfolio_runtime(database, scope=ConfigurationScope.CAPITAL).portfolio.snapshot()
        SnapshotRepository(database).append_portfolio(snapshot)
        return snapshot

    _ = run_operator_command(run, heading="Portfolio snapshot", human_value=_portfolio_human_value)


def _portfolio_human_value(value: object) -> object:
    if not isinstance(value, BaseModel):
        return value
    payload = cast("dict[str, object]", value.model_dump(mode="json"))
    snapshot_payload = cast("dict[str, object]", payload.get("payload", {}))
    environment = str(snapshot_payload.get("broker_environment", "unknown")).upper()
    account_id = snapshot_payload.get("account_id")
    if account_id is not None:
        account = str(account_id)
        snapshot_payload["account_id"] = f"****{account[-4:]}"
        payload["payload"] = snapshot_payload
    return {"environment": environment, **payload}


@portfolio_app.command(name="review")
def portfolio_review() -> None:
    """Run A5 portfolio planning from current durable intelligence."""

    def run() -> object:
        database = _database()
        return execute_portfolio_review(
            database=database,
            config=load_application_config(scope=ConfigurationScope.CAPITAL),
            paths=RepositoryPaths.from_data_root(DATA_ROOT),
            requested_as_of=None,
            implementation_version=APP_VERSION,
        )

    _ = run_operator_command(run, heading="Portfolio review")


def _plan(plan_id: str) -> PortfolioPlan:
    plan = PortfolioPlanRepository(_database()).get(plan_id)
    if plan is None:
        raise typer.BadParameter(f"unknown portfolio plan {plan_id!r}")
    return plan


def _required_option_text(value: str, *, name: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise typer.BadParameter(f"{name} cannot be blank")
    return normalized


def _optional_option_text(value: str | None, *, name: str) -> str | None:
    return None if value is None else _required_option_text(value, name=name)


@plan_app.command(name="show")
def plan_show(plan_id: str) -> None:
    """Show one exact-hash portfolio plan."""
    _ = run_operator_command(lambda: _plan(plan_id), heading="Portfolio plan")


@plan_app.command(name="approve")
def plan_approve(
    plan_id: str,
    actor: Annotated[str, typer.Option("--actor")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Approve one exact, unexpired plan hash."""

    def run() -> object:
        database = _database()
        config = load_application_config(scope=ConfigurationScope.EXECUTION)
        execution_policy = _execution_policy(config)
        runtime = _portfolio_runtime(database, scope=ConfigurationScope.EXECUTION)
        repository = SqliteExecutionAuthorityRepository(database)
        portfolio = runtime.portfolio.snapshot()
        return record_plan_decision(
            PortfolioPlanRepository(database),
            repository,
            plan_id=plan_id,
            decision=ApprovalDecision.APPROVED,
            decided_at=datetime.now(tz=UTC),
            actor=_required_option_text(actor, name="actor"),
            reason=_optional_option_text(reason, name="reason"),
            approval_binding=ApprovalBinding(
                execution_config_hash=canonical_config_hash(config.require_execution()),
                execution_policy=execution_policy,
                portfolio=portfolio,
                committed_turnover=repository.committed_turnover_excluding_plan(plan_id, datetime.now(tz=UTC)),
            ),
        )

    _ = run_operator_command(run, heading="Plan approval")


@plan_app.command(name="reject")
def plan_reject(
    plan_id: str,
    actor: Annotated[str, typer.Option("--actor")],
    reason: Annotated[str, typer.Option("--reason")],
) -> None:
    """Reject one exact plan hash with a reason."""

    def run() -> object:
        database = _database()
        repository = SqliteExecutionAuthorityRepository(database)
        return record_plan_decision(
            PortfolioPlanRepository(database),
            repository,
            plan_id=plan_id,
            decision=ApprovalDecision.REJECTED,
            decided_at=datetime.now(tz=UTC),
            actor=_required_option_text(actor, name="actor"),
            reason=_required_option_text(reason, name="reason"),
        )

    _ = run_operator_command(run, heading="Plan rejection")


@plan_app.command(name="execute")
def plan_execute(plan_id: str) -> None:
    """Execute one plan through the scoped A6 gateway."""

    def run() -> object:
        database = _database()
        config = load_application_config()
        gateway = _portfolio_runtime(database).execution
        if gateway is None:
            raise RuntimeError("execution configuration did not compose an A6 gateway")
        return execute_plan(plan_id, _execution_policy(config), gateway)

    _ = run_operator_command(run, heading="Execution receipt")


def _execution_policy(config: ApplicationConfig) -> ExecutionPolicy:
    execution = config.require_execution()
    return ExecutionPolicy(
        policy_version=execution.policy_version,
        broker_environment=execution.broker_environment,
        execution_mode=execution.execution_mode,
        allowed_asset_classes=execution.allowed_asset_classes,
        maximum_order_notional=execution.maximum_order_notional,
        maximum_daily_turnover=execution.maximum_daily_turnover,
    )


@execution_app.command(name="status")
def execution_status() -> None:
    """Show global execution authority."""
    _ = run_operator_command(
        lambda: SqliteExecutionAuthorityRepository(_database()).get_control_state(),
        heading="Execution authority",
    )


@execution_app.command(name="disable")
def execution_disable(
    actor: Annotated[str, typer.Option("--actor")],
    reason: Annotated[str, typer.Option("--reason")],
) -> None:
    """Disable every capital write."""
    _ = run_operator_command(
        lambda: disable_execution(
            SqliteExecutionAuthorityRepository(_database()),
            changed_at=datetime.now(tz=UTC),
            actor=_required_option_text(actor, name="actor"),
            reason=_required_option_text(reason, name="reason"),
        ),
        heading="Execution disabled",
    )


@execution_app.command(name="enable")
def execution_enable(
    actor: Annotated[str, typer.Option("--actor")],
    reason: Annotated[str, typer.Option("--reason")],
    policy_version: Annotated[str, typer.Option("--policy-version")],
    confirmed: Annotated[bool, typer.Option("--confirmed")] = False,
) -> None:
    """Enable execution under one explicit policy after confirmation."""
    _ = run_operator_command(
        lambda: enable_execution(
            SqliteExecutionAuthorityRepository(_database()),
            changed_at=datetime.now(tz=UTC),
            actor=_required_option_text(actor, name="actor"),
            reason=_required_option_text(reason, name="reason"),
            policy_version=_required_option_text(policy_version, name="policy-version"),
            confirmed=confirmed,
        ),
        heading="Execution enabled",
    )


@app.command()
def replay(run_id: str) -> None:
    """Replay exact immutable artifacts without providers, models, or broker clients."""

    def run() -> ReplayReport:
        database = _read_database()
        store = DurableRunRepository(database)
        resolved = RunRepository(RepositoryPaths.from_data_root(DATA_ROOT)).resolve(run_id)
        record = store.get_run(run_id)
        terminal_event = store.terminal_event_for_run(run_id)
        if terminal_event is None:
            raise RuntimeError(f"Run {run_id!r} is not terminal and cannot be replayed.")
        state = make_replay_node(store)(
            {
                "run_id": run_id,
                "run_dir": str(resolved.path),
                "requested_as_of": record.requested_as_of,
                "run_started_at": record.started_at,
                "replay": True,
            }
        )
        return ReplayReport(
            run_id=run_id,
            requested_as_of=record.requested_as_of,
            started_at=record.started_at,
            terminal_status=terminal_event.status.value,
            completed_at=terminal_event.completed_at,
            completed_stages=state.get("completed_stages", ()),
            artifact_ids=state.get("artifact_ids", ()),
            decision_at=state.get("decision_at"),
            plan_id=state.get("plan_id"),
            plan_hash=state.get("plan_hash"),
            report_id=state.get("report_id"),
        )

    _ = run_operator_command(run, heading="Replay")


@app.command()
def doctor() -> None:
    """Inspect local configuration and storage without providers, secrets, or writes."""
    _ = run_operator_command(_doctor_report, heading="Doctor")


def _doctor_report() -> DoctorReport:
    paths = RepositoryPaths.from_data_root(DATA_ROOT)
    checks: list[DoctorCheck] = []
    project_root = Path.cwd().resolve()
    configuration_paths = {
        "sources": default_sources_config_path().resolve(strict=False),
        "strategy": default_strategy_config_path().resolve(strict=False),
        "execution": default_execution_config_path().resolve(strict=False),
        "secretspec": default_config_path().resolve(strict=False),
    }
    for name, path in configuration_paths.items():
        exists = path.is_file()
        checks.append(
            DoctorCheck(
                category=f"configuration.{name}",
                status="ok" if exists else "missing",
                detail=f"{path} {'exists' if exists else 'does not exist'}",
            )
        )
    absolute_paths = {
        "data": paths.data_root.resolve(strict=False),
        "database": paths.database_path.resolve(strict=False),
        "assets": paths.assets_root.resolve(strict=False),
        "runs": paths.runs_root.resolve(strict=False),
        "reports": paths.reports_root.resolve(strict=False),
    }
    for name, path in absolute_paths.items():
        writable = _path_is_writable_or_creatable(path)
        checks.append(
            DoctorCheck(
                category=f"path.{name}",
                status="ok" if writable else "unwritable",
                detail=f"{path} is {'writable or creatable' if writable else 'not writable'}",
            )
        )
    inspection = inspect_database(paths.database_path)
    database_ok = inspection.state in {DatabaseSchemaState.CURRENT, DatabaseSchemaState.MISSING}
    checks.append(
        DoctorCheck(
            category="storage.schema",
            status="ok" if database_ok else inspection.state.value,
            detail=inspection.detail,
        )
    )
    configured_model: str | None = None
    enabled_sources: tuple[str, ...] = ()
    enabled_providers: tuple[str, ...] = ()
    portfolio_environment: str | None = None
    execution_environment: str | None = None
    try:
        configuration = load_application_config(scope=ConfigurationScope.EXECUTION)
        configured_model = configuration.intelligence.llm_model
        enabled = tuple(source for source in configuration.sources.sources if source.enabled)
        enabled_sources = tuple(source.source_id for source in enabled)
        enabled_providers = tuple(sorted({"openai", *(source.adapter_name for source in enabled)}))
        portfolio_environment = configuration.require_strategy().portfolio_environment.value
        execution_environment = configuration.require_execution().broker_environment.value
        checks.append(DoctorCheck(category="configuration.validation", status="ok", detail="configuration is valid"))
    except ConfigurationError as error:
        checks.append(
            DoctorCheck(
                category="configuration.validation",
                status="invalid",
                detail=" ".join(str(error).split())[:500],
            )
        )
    return DoctorReport(
        checked_at=datetime.now(tz=UTC),
        healthy=database_ok and all(check.status == "ok" for check in checks if check.category != "storage.schema"),
        project_root=str(project_root),
        configuration_paths={name: str(path) for name, path in configuration_paths.items()},
        data_path=str(absolute_paths["data"]),
        database_path=str(absolute_paths["database"]),
        asset_path=str(absolute_paths["assets"]),
        run_path=str(absolute_paths["runs"]),
        report_path=str(absolute_paths["reports"]),
        application_version=APP_VERSION,
        database_release=inspection.metadata_release,
        migration_required=inspection.state is DatabaseSchemaState.EXACT_PREDECESSOR,
        secretspec_profile=os.environ.get(SECRETSPEC_PROFILE_ENV, DEFAULT_SECRETSPEC_PROFILE),
        secretspec_manifest_path=str(configuration_paths["secretspec"]),
        configured_model=configured_model,
        enabled_sources=enabled_sources,
        enabled_providers=enabled_providers,
        portfolio_environment=portfolio_environment,
        execution_environment=execution_environment,
        checks=tuple(checks),
    )


def _path_is_writable_or_creatable(path: Path) -> bool:
    candidate = path if path.exists() else path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.exists() and os.access(candidate, os.W_OK)


if __name__ == "__main__":
    app()  # pragma: no cover
