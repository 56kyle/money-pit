"""Command-line interface for persistent money_pit intelligence."""

from datetime import UTC
from datetime import datetime

import typer

from money_pit.claims.cli import claims_app
from money_pit.composition import execute_harness_run
from money_pit.config import ApplicationConfig
from money_pit.config import ConfigurationScope
from money_pit.config import canonical_config_hash
from money_pit.config import load_application_config
from money_pit.constants import APP_VERSION
from money_pit.constants import DATA_ROOT
from money_pit.execution_control.gateway import execute_plan
from money_pit.execution_control.models import ApprovalDecision
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.execution_control.service import ApprovalBinding
from money_pit.execution_control.service import disable_execution
from money_pit.execution_control.service import enable_execution
from money_pit.execution_control.service import record_plan_decision
from money_pit.pipeline.chain import Stage
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
from money_pit.sources.cli import source_app
from money_pit.storage.database import Database
from money_pit.storage.runs import RunRepository as DurableRunRepository


app = typer.Typer(help="Persistent point-in-time investment research.")
research_app = typer.Typer(help="Inspect bounded durable research.")
theses_app = typer.Typer(help="Inspect persistent thesis history.")
portfolio_app = typer.Typer(help="Capture and review portfolio decisions.")
plan_app = typer.Typer(help="Inspect, decide, and execute exact-hash plans.")
execution_app = typer.Typer(help="Manage global execution authority.")
app.add_typer(source_app, name="source")
app.add_typer(research_app, name="research")
app.add_typer(claims_app, name="claims")
app.add_typer(theses_app, name="theses")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(plan_app, name="plan")
app.add_typer(execution_app, name="execution")


def _database() -> Database:
    database = Database(RepositoryPaths.from_data_root(DATA_ROOT).database_path)
    database.initialize()
    return database


def _portfolio_runtime(
    database: Database,
    *,
    scope: ConfigurationScope = ConfigurationScope.EXECUTION,
) -> PortfolioRuntime:
    config = load_application_config(scope=scope)
    paths = RepositoryPaths.from_data_root(DATA_ROOT)
    return build_portfolio_runtime(
        database=database,
        config=config,
        reports_root=paths.reports_root,
        processor_versions={"builtin_evidence_processors": APP_VERSION},
        model_versions={"llm": config.environment.llm_model},
        prompt_versions={"portfolio": APP_VERSION},
        implementation_version=APP_VERSION,
    )


def _run_harness(*, source: str | None, as_of: datetime | None, through: Stage) -> object:
    database = _database()
    scope = (
        ConfigurationScope.EXECUTION
        if through is Stage.A6
        else ConfigurationScope.CAPITAL
        if through is Stage.A5
        else ConfigurationScope.INTELLIGENCE
    )
    config = load_application_config(scope=scope)
    paths = RepositoryPaths.from_data_root(DATA_ROOT)
    return execute_harness_run(
        database=database,
        config=config,
        paths=paths,
        source_id=source,
        requested_as_of=as_of,
        through=through,
        implementation_version=APP_VERSION,
    )


@app.command()
def run(
    source: str | None = None,
    as_of: datetime | None = None,
    through: Stage | None = None,
) -> None:
    """Process pending durable evidence through the selected harness stage."""
    result = _run_harness(source=source, as_of=as_of, through=through or Stage.A6)
    typer.echo(str(result))


@research_app.command(name="list")
def research_list() -> None:
    """List durable research sessions."""
    for session in ResearchRepository(_database()).sessions():
        scope = session.scope
        if isinstance(scope, CandidateThesisResearchScope):
            subject = scope.candidate_thesis_id
        elif isinstance(scope, CanonicalClaimResearchScope):
            subject = scope.canonical_claim_key
        else:
            subject = scope.observation_id
        stop_reason = "" if session.stop_reason is None else session.stop_reason.value
        typer.echo(f"{session.session_id}\t{scope.kind}\t{subject}\t{session.status.value}\t{stop_reason}")


@research_app.command(name="show")
def research_show(session_id: str) -> None:
    """Show one durable research session."""
    session = ResearchRepository(_database()).session(session_id)
    if session is None:
        raise typer.BadParameter(f"unknown research session {session_id!r}")
    typer.echo(session.model_dump_json(indent=2))


@theses_app.command(name="list")
def theses_list(as_of: datetime | None = None) -> None:
    """List current point-in-time thesis revisions."""
    cutoff = as_of or datetime.now(tz=UTC)
    for revision in ThesisRepository(_database()).revisions_as_of(as_of=cutoff):
        typer.echo(f"{revision.thesis_id}\t{revision.revision_id}\t{revision.status.value}")


@theses_app.command(name="show")
def theses_show(revision_id: str) -> None:
    """Show one immutable thesis revision."""
    revision = ThesisRepository(_database()).get_revision(revision_id)
    if revision is None:
        raise typer.BadParameter(f"unknown thesis revision {revision_id!r}")
    typer.echo(revision.model_dump_json(indent=2))


@theses_app.command(name="review")
def theses_review() -> None:
    """Run thesis review through A4."""
    typer.echo(str(_run_harness(source=None, as_of=None, through=Stage.A4)))


@portfolio_app.command(name="snapshot")
def portfolio_snapshot() -> None:
    """Capture and persist the current read-only broker portfolio state."""
    database = _database()
    snapshot = _portfolio_runtime(database, scope=ConfigurationScope.CAPITAL).portfolio.snapshot()
    SnapshotRepository(database).append_portfolio(snapshot)
    typer.echo(snapshot.model_dump_json(indent=2))


@portfolio_app.command(name="review")
def portfolio_review() -> None:
    """Start a portfolio-wide review at A5."""
    typer.echo(str(_run_harness(source=None, as_of=None, through=Stage.A5)))


def _plan(plan_id: str) -> PortfolioPlan:
    plan = PortfolioPlanRepository(_database()).get(plan_id)
    if plan is None:
        raise typer.BadParameter(f"unknown portfolio plan {plan_id!r}")
    return plan


@plan_app.command(name="show")
def plan_show(plan_id: str) -> None:
    """Show one exact-hash portfolio plan."""
    typer.echo(_plan(plan_id).model_dump_json(indent=2))


@plan_app.command(name="approve")
def plan_approve(plan_id: str, actor: str, reason: str | None = None) -> None:
    """Approve one exact, unexpired plan hash."""
    database = _database()
    config = load_application_config(scope=ConfigurationScope.EXECUTION)
    execution_policy = _execution_policy(config)
    runtime = _portfolio_runtime(database, scope=ConfigurationScope.EXECUTION)
    repository = SqliteExecutionAuthorityRepository(database)
    portfolio = runtime.portfolio.snapshot()
    record = record_plan_decision(
        PortfolioPlanRepository(database),
        repository,
        plan_id=plan_id,
        decision=ApprovalDecision.APPROVED,
        decided_at=datetime.now(tz=UTC),
        actor=actor,
        reason=reason,
        approval_binding=ApprovalBinding(
            execution_config_hash=canonical_config_hash(config.require_execution()),
            execution_policy=execution_policy,
            portfolio=portfolio,
            committed_turnover=repository.committed_turnover_excluding_plan(plan_id, datetime.now(tz=UTC)),
        ),
    )
    typer.echo(record.model_dump_json())


@plan_app.command(name="reject")
def plan_reject(plan_id: str, actor: str, reason: str) -> None:
    """Reject one exact plan hash with a reason."""
    database = _database()
    repository = SqliteExecutionAuthorityRepository(database)
    record = record_plan_decision(
        PortfolioPlanRepository(database),
        repository,
        plan_id=plan_id,
        decision=ApprovalDecision.REJECTED,
        decided_at=datetime.now(tz=UTC),
        actor=actor,
        reason=reason,
    )
    typer.echo(record.model_dump_json())


@plan_app.command(name="execute")
def plan_execute(plan_id: str) -> None:
    """Execute one plan through the scoped A6 gateway."""
    database = _database()
    config = load_application_config()
    gateway = _portfolio_runtime(database).execution
    if gateway is None:
        raise RuntimeError("execution configuration did not compose an A6 gateway")
    receipt = execute_plan(
        plan_id,
        _execution_policy(config),
        gateway,
    )
    typer.echo(receipt.model_dump_json(indent=2))


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
    typer.echo(SqliteExecutionAuthorityRepository(_database()).get_control_state().model_dump_json(indent=2))


@execution_app.command(name="disable")
def execution_disable(actor: str, reason: str) -> None:
    """Disable every capital write."""
    state = disable_execution(
        SqliteExecutionAuthorityRepository(_database()), changed_at=datetime.now(tz=UTC), actor=actor, reason=reason
    )
    typer.echo(state.model_dump_json())


@execution_app.command(name="enable")
def execution_enable(actor: str, reason: str, policy_version: str, confirmed: bool = False) -> None:
    """Enable execution under one explicit policy after confirmation."""
    state = enable_execution(
        SqliteExecutionAuthorityRepository(_database()),
        changed_at=datetime.now(tz=UTC),
        actor=actor,
        reason=reason,
        policy_version=policy_version,
        confirmed=confirmed,
    )
    typer.echo(state.model_dump_json())


@app.command()
def replay(run_id: str) -> None:
    """Replay exact immutable artifacts without providers, models, or broker clients."""
    database = _database()
    store = DurableRunRepository(database)
    resolved = RunRepository(RepositoryPaths.from_data_root(DATA_ROOT)).resolve(run_id)
    record = store.get_run(run_id)
    state = make_replay_node(store)(
        {
            "run_id": run_id,
            "run_dir": str(resolved.path),
            "requested_as_of": record.requested_as_of,
            "run_started_at": record.started_at,
            "replay": True,
        }
    )
    typer.echo(str(state))


if __name__ == "__main__":
    app()  # pragma: no cover
