"""Command-line interface."""

import datetime
import json
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Annotated

import typer
from pydantic import ValidationError

from money_pit.adapters.text import TextAdapter
from money_pit.adapters.text_llm import TextPayload
from money_pit.adapters.text_llm import make_text_llm_agent
from money_pit.adapters.text_source import EmptyThesisError
from money_pit.adapters.text_source import ThesisFileNotReadableError
from money_pit.adapters.text_source import build_text_payload
from money_pit.adapters.text_source import read_thesis
from money_pit.adapters.video import VideoAdapter
from money_pit.adapters.video_llm import VideoPayload
from money_pit.adapters.video_llm import make_video_llm_agent
from money_pit.claims.cli import claims_app
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.config import load_config
from money_pit.config import resolve_alpaca_credentials
from money_pit.constants import DATA_ROOT
from money_pit.constants import FILE_SAFE_DATETIME_FORMAT
from money_pit.constants import REPORTS_DIRNAME
from money_pit.constants import SIGNALS_DIRNAME
from money_pit.constants import default_processed_episodes_path
from money_pit.constants import source_id_to_dirname
from money_pit.execution_control.models import ExecutionControlState
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_working_dir
from money_pit.ingestion.pipeline import IngestionSeams
from money_pit.ingestion.pipeline import ingest_video
from money_pit.ingestion.pipeline import production_seams
from money_pit.legacy.daily_show import LegacyDailyShowImporter
from money_pit.log import configure_file_logging
from money_pit.mcp.clients import list_write_tools
from money_pit.mcp.constants import PLACE_STOCK_ORDER_TOOL
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_PATH
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.orchestration import production_deps
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.pipeline.stages import StageError
from money_pit.pipeline.stages import run_stage
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import RejectionRecord
from money_pit.plans.lifecycle import approve_plan
from money_pit.plans.lifecycle import reject_plan
from money_pit.plans.repository import PortfolioPlanRepository
from money_pit.reports.portfolio import PortfolioReview
from money_pit.reports.portfolio import ReportReadError
from money_pit.reports.portfolio import load_portfolio_review
from money_pit.reports.portfolio import write_report_bundle
from money_pit.runs.paths import RepositoryPaths
from money_pit.runs.repository import ResolvedRun
from money_pit.runs.repository import RunManifestReadError
from money_pit.runs.repository import RunNotFoundError
from money_pit.runs.repository import RunRepository
from money_pit.scheduler.channel import fetch_latest_video_id
from money_pit.scheduler.channel import make_requests_http_get
from money_pit.scheduler.runner import RunLatestOutcome
from money_pit.scheduler.runner import RunLatestResult
from money_pit.scheduler.runner import SchedulerConfigError
from money_pit.scheduler.runner import run_latest_once
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.sources.cli import source_app
from money_pit.storage.database import Database
from money_pit.storage.errors import StorageError


if TYPE_CHECKING:
    from mcp.types import Tool

    from money_pit.schemas.enums import TerminalState
    from money_pit.schemas.signals import SignalSet


app: typer.Typer = typer.Typer()

_JSON_INDENT: int = 2
portfolio_app: typer.Typer = typer.Typer(help="Build static, non-executing portfolio reviews.")
app.add_typer(portfolio_app, name="portfolio")
plan_app: typer.Typer = typer.Typer(help="Inspect and decide durable portfolio plans.")
execution_app: typer.Typer = typer.Typer(help="Inspect or disable capital-write authority.")
app.add_typer(plan_app, name="plan")
app.add_typer(execution_app, name="execution")
app.add_typer(source_app, name="source")
app.add_typer(claims_app, name="claims")
_NO_TERMINAL_STATE_LABEL: str = "none"
_NO_COMPLETED_STEPS_LABEL: str = "none"

_THROUGH_HELP: str = "Stop the run after this stage instead of carrying on to execution; the run directory still holds every artifact produced up to that point."
_RUN_DIR_HELP: str = "The existing run directory to read inputs from and write this stage's artifacts back into."
_DEFAULT_PORTFOLIO_REVIEW_INPUT: Path = DATA_ROOT / "portfolio-review.json"
_DEFAULT_PORTFOLIO_REPORT_DIRECTORY: Path = DATA_ROOT / REPORTS_DIRNAME / "portfolio-review"


def _mint_slug() -> str:
    """Mint a UTC run slug in the repo's filesystem-safe datetime format."""
    return datetime.datetime.now(tz=datetime.timezone.utc).strftime(FILE_SAFE_DATETIME_FORMAT)


def _ingest_to_signal_file(
    url: str,
    slug: str,
    cache_dir: Path,
    signals_dir: Path,
    seams: IngestionSeams,
    agent: Callable[[VideoPayload], SignalSetDraft],
    *,
    max_frames: int,
) -> Path:
    """Ingest a video URL into a persisted SignalSet JSON file and return that file's path."""
    payload: VideoPayload = ingest_video(url, slug, cache_dir, seams, max_frames=max_frames)
    signal_set: SignalSet = VideoAdapter(agent=agent, cache_dir=cache_dir).process(payload)
    signals_dir.mkdir(parents=True, exist_ok=True)
    signal_path: Path = signals_dir / f"{source_id_to_dirname(signal_set.source_ref.source_id)}.json"
    _ = signal_path.write_text(signal_set.model_dump_json(indent=_JSON_INDENT), encoding="utf-8")
    return signal_path


def _echo_outcome(headline: str, state: PipelineState) -> None:
    """Echo where the run's artifacts landed and what conclusion, if any, it reached, raising ValueError when the state names no run directory."""
    terminal_state: TerminalState | None = state.get("terminal_state")
    label: str = terminal_state.value if terminal_state is not None else _NO_TERMINAL_STATE_LABEL
    working_dir: Path = require_working_dir(state)
    typer.echo(f"{headline} Terminal state: {label}. Run directory: {working_dir}")


def _run_signals_dir(signals_dir: Path, config: Config, *, through: Stage | None) -> PipelineState:
    """Execute the pipeline over a prepared signals directory on the production (paper) deps, stopping after `through`."""
    return run_pipeline(signals_dir, overrides=production_deps(config), through=through)


def _run_url(url: str, config: Config, *, through: Stage | None) -> PipelineState:
    """Ingest one video URL and execute the pipeline over it, stopping after `through` and returning the state reached."""
    slug: str = _mint_slug()
    seams: IngestionSeams = production_seams(config)
    agent: Callable[[VideoPayload], SignalSetDraft] = make_video_llm_agent(config)
    signals_dir: Path = Path(tempfile.mkdtemp())
    _ = _ingest_to_signal_file(
        url,
        slug,
        config.ingest_cache_dir,
        signals_dir,
        seams,
        agent,
        max_frames=config.keyframe_max_frames,
    )
    return _run_signals_dir(signals_dir, config, through=through)


@app.callback()
def _configure() -> None:
    """Configure process-wide file logging before any command runs."""
    _ = configure_file_logging()


@app.command(name="money-pit")
def main() -> None:
    """Money Pit."""


@app.command(name="pin-order-schema")
def pin_order_schema() -> None:
    """Introspect the live place_stock_order tool and pin its inputSchema to the committed schema path."""
    try:
        credentials: AlpacaCredentials = resolve_alpaca_credentials(load_config())
    except CredentialResolutionError as error:
        typer.echo(f"Cannot resolve Alpaca credentials: {error}", err=True)
        raise typer.Exit(code=1) from error

    try:
        tools: list[Tool] = list_write_tools(credentials)
    except Exception as error:
        typer.echo(f"Failed to introspect the Alpaca MCP write server: {error}", err=True)
        raise typer.Exit(code=1) from error

    schema: dict[str, object] | None = next(
        (tool.inputSchema for tool in tools if tool.name == PLACE_STOCK_ORDER_TOOL), None
    )
    if schema is None:
        typer.echo(
            f"Tool {PLACE_STOCK_ORDER_TOOL!r} is absent from the Alpaca MCP write server;"
            + " refusing to write a partial schema.",
            err=True,
        )
        raise typer.Exit(code=1)

    pinned: dict[str, object] = dict(schema)
    _ = pinned.pop(ALPACA_ORDER_SCHEMA_STUB_SENTINEL, None)
    _ = ALPACA_ORDER_SCHEMA_PATH.write_text(json.dumps(pinned, indent=2), encoding="utf-8")
    typer.echo(f"Pinned {PLACE_STOCK_ORDER_TOOL} inputSchema to {ALPACA_ORDER_SCHEMA_PATH}")


@app.command()
def ingest(url: str) -> None:
    """Ingest a single video URL into a boundary-0 signal file under the ingest cache."""
    config: Config = load_config()
    slug: str = _mint_slug()
    seams: IngestionSeams = production_seams(config)
    agent: Callable[[VideoPayload], SignalSetDraft] = make_video_llm_agent(config)
    signal_path: Path = _ingest_to_signal_file(
        url,
        slug,
        config.ingest_cache_dir,
        config.ingest_cache_dir / SIGNALS_DIRNAME,
        seams,
        agent,
        max_frames=config.keyframe_max_frames,
    )
    typer.echo(f"Wrote signal file to {signal_path}")


@app.command(name="stage")
def stage(name: Stage, run_dir: Annotated[Path, typer.Option(help=_RUN_DIR_HELP)]) -> None:
    """Run one observation stage on its own against an existing run directory, placing no orders and sending no mail."""
    config: Config = load_config()
    try:
        state: PipelineState = run_stage(name, run_dir, config)
    except StageError as error:
        typer.echo(f"Cannot run stage {name.value}: {error}", err=True)
        raise typer.Exit(code=1) from error

    completed: str = ", ".join(state.get("completed_steps") or []) or _NO_COMPLETED_STEPS_LABEL
    typer.echo(f"Stage {name.value} complete. Completed steps: {completed}. Run directory: {run_dir}")


def _resolve_replay_snapshot(run_id: str, paths: RepositoryPaths) -> ResolvedRun:
    """Index legacy directories and resolve one immutable artifact snapshot."""
    database: Database = Database(paths.database_path)
    database.initialize()
    _ = LegacyDailyShowImporter(database, paths.legacy_daily_show_root).index()
    return RunRepository(paths, database).resolve(run_id)


@portfolio_app.command(name="review")
def portfolio_review(
    review_input: Annotated[
        Path,
        typer.Option("--input", help="A complete typed PortfolioReview JSON snapshot."),
    ] = _DEFAULT_PORTFOLIO_REVIEW_INPUT,
    report_directory: Annotated[
        Path,
        typer.Option("--report-directory", help="Directory for static JSON, Markdown, and HTML reports."),
    ] = _DEFAULT_PORTFOLIO_REPORT_DIRECTORY,
) -> None:
    """Validate a portfolio review snapshot and render its static report bundle."""
    try:
        review: PortfolioReview = load_portfolio_review(review_input)
        report_paths: tuple[Path, Path, Path] = write_report_bundle(review, report_directory)
    except (ReportReadError, OSError) as error:
        typer.echo(f"Cannot build portfolio review: {error}", err=True)
        raise typer.Exit(code=1) from error
    rendered_paths: str = ", ".join(str(path) for path in report_paths)
    typer.echo(f"Portfolio review complete: {rendered_paths}")


def _execution_authority_repository() -> SqliteExecutionAuthorityRepository:
    """Initialize and return the durable SQLite execution-authority repository."""
    paths: RepositoryPaths = RepositoryPaths.from_data_root()
    database: Database = Database(paths.database_path)
    database.initialize()
    return SqliteExecutionAuthorityRepository(database)


def _portfolio_plan_repository() -> PortfolioPlanRepository:
    """Initialize and return the durable immutable-plan repository."""
    paths: RepositoryPaths = RepositoryPaths.from_data_root()
    database: Database = Database(paths.database_path)
    database.initialize()
    return PortfolioPlanRepository(database)


def _require_cli_plan(repository: SqliteExecutionAuthorityRepository, plan_id: str) -> PortfolioPlan:
    plan: PortfolioPlan | None = repository.get(plan_id)
    if plan is None:
        typer.echo(f"Portfolio plan {plan_id!r} does not exist.", err=True)
        raise typer.Exit(code=1)
    return plan


@plan_app.command(name="import")
def plan_import(input_path: Path) -> None:
    """Validate and persist a fully constructed optimizer-derived PortfolioPlan."""
    try:
        plan: PortfolioPlan = PortfolioPlan.model_validate_json(input_path.read_text(encoding="utf-8"))
        _portfolio_plan_repository().append(plan)
    except (OSError, ValidationError, StorageError) as error:
        typer.echo(f"Cannot import portfolio plan: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Imported plan {plan.payload.plan_id} at hash {plan.plan_hash}.")


@plan_app.command(name="show")
def plan_show(plan_id: str) -> None:
    """Show one validated durable portfolio plan as JSON."""
    try:
        plan: PortfolioPlan = _require_cli_plan(_execution_authority_repository(), plan_id)
    except StorageError as error:
        typer.echo(f"Cannot show portfolio plan: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(plan.model_dump_json(indent=_JSON_INDENT))


@plan_app.command(name="approve")
def plan_approve(plan_id: str, actor: str = "operator") -> None:
    """Approve one exact portfolio-plan hash without constructing a broker writer."""
    try:
        repository: SqliteExecutionAuthorityRepository = _execution_authority_repository()
        plan: PortfolioPlan = _require_cli_plan(repository, plan_id)
        record: ApprovalRecord = approve_plan(
            plan,
            decision_id=str(uuid.uuid4()),
            decided_at=datetime.datetime.now(tz=datetime.timezone.utc),
            decided_by=actor,
        )
        repository.append(record)
    except StorageError as error:
        typer.echo(f"Cannot approve portfolio plan: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Approved plan {record.plan_id} at hash {record.plan_hash}.")


@plan_app.command(name="reject")
def plan_reject(plan_id: str, reason: str, actor: str = "operator") -> None:
    """Reject one exact portfolio-plan hash without constructing a broker writer."""
    try:
        repository: SqliteExecutionAuthorityRepository = _execution_authority_repository()
        plan: PortfolioPlan = _require_cli_plan(repository, plan_id)
        record: RejectionRecord = reject_plan(
            plan,
            decision_id=str(uuid.uuid4()),
            decided_at=datetime.datetime.now(tz=datetime.timezone.utc),
            decided_by=actor,
            reason=reason,
        )
        repository.append(record)
    except StorageError as error:
        typer.echo(f"Cannot reject portfolio plan: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Rejected plan {record.plan_id} at hash {record.plan_hash}.")


@execution_app.command(name="status")
def execution_status() -> None:
    """Show the durable global execution-control state."""
    state: ExecutionControlState = _execution_authority_repository().get_control_state()
    typer.echo(state.model_dump_json(indent=_JSON_INDENT))


@execution_app.command(name="disable")
def execution_disable(reason: str, actor: str = "operator") -> None:
    """Durably disable every capital write."""
    repository: SqliteExecutionAuthorityRepository = _execution_authority_repository()
    state: ExecutionControlState = ExecutionControlState(
        disabled=True,
        changed_at=datetime.datetime.now(tz=datetime.timezone.utc),
        actor=actor,
        reason=reason,
    )
    repository.disable(state)
    typer.echo("Execution disabled.")


@app.command(name="execute")
def execute_plan(plan_id: str) -> None:
    """Fail closed until a trusted live state provider can authorize the approved plan."""
    config: Config = load_config()
    repository: SqliteExecutionAuthorityRepository = _execution_authority_repository()
    plan: PortfolioPlan = _require_cli_plan(repository, plan_id)
    state: ExecutionControlState = repository.get_control_state()
    if state.disabled:
        typer.echo("Execution is disabled by the global kill switch.", err=True)
        raise typer.Exit(code=1)
    if config.execution_mode is ExecutionMode.OBSERVE:
        typer.echo("Observe mode does not grant execution authority.", err=True)
        raise typer.Exit(code=1)
    if (
        config.execution_policy_version is None
        or config.maximum_order_notional is None
        or config.maximum_daily_turnover is None
    ):
        typer.echo("Execution policy is incomplete; no broker writer was created.", err=True)
        raise typer.Exit(code=1)
    decision = repository.latest_for(plan.payload.plan_id)
    exact_approval: bool = (
        isinstance(decision, ApprovalRecord)
        and decision.plan_id == plan.payload.plan_id
        and decision.plan_hash == plan.plan_hash
        and plan.payload.created_at <= decision.decided_at < plan.payload.expires_at
    )
    if config.execution_mode is ExecutionMode.APPROVAL_REQUIRED and not exact_approval:
        typer.echo("The exact plan hash lacks current operator approval.", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        "No trusted live AuthorizationContext provider is configured; "
        "execution stopped before broker write-client creation.",
        err=True,
    )
    raise typer.Exit(code=1)


@execution_app.command(name="enable")
def execution_enable(reason: str, actor: str = "operator") -> None:
    """Enable the global switch without authorizing any portfolio plan."""
    repository: SqliteExecutionAuthorityRepository = _execution_authority_repository()
    state: ExecutionControlState = ExecutionControlState(
        disabled=False,
        changed_at=datetime.datetime.now(tz=datetime.timezone.utc),
        actor=actor,
        reason=reason,
    )
    repository.enable(state)
    typer.echo("Execution enabled; all plan and live-state gates remain required.")


@app.command()
def replay(run_id: str) -> None:
    """Resolve a point-in-time run artifact snapshot without re-executing stages."""
    try:
        resolved: ResolvedRun = _resolve_replay_snapshot(
            run_id,
            RepositoryPaths.from_data_root(),
        )
    except (RunNotFoundError, RunManifestReadError) as error:
        typer.echo(f"Cannot resolve replay snapshot: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Resolved {resolved.origin} point-in-time artifact snapshot {resolved.run_id} "
        f"at {resolved.path}. No stages were re-executed."
    )


@app.command()
def run(url: str, through: Annotated[Stage | None, typer.Option(help=_THROUGH_HELP)] = None) -> None:
    """Ingest a URL into a fresh ephemeral signals directory and execute the pipeline over it."""
    config: Config = load_config()
    state: PipelineState = _run_url(url, config, through=through)
    _echo_outcome("Run complete.", state)


@app.command(name="analyze-text")
def analyze_text(
    file: Path,
    title: str | None = None,
    through: Annotated[Stage | None, typer.Option(help=_THROUGH_HELP)] = None,
) -> None:
    """Classify a plain-text thesis file into signals and execute the pipeline over it (paper)."""
    config: Config = load_config()
    try:
        body: str = read_thesis(file)
    except (ThesisFileNotReadableError, EmptyThesisError) as error:
        typer.echo(f"Cannot analyze thesis: {error}", err=True)
        raise typer.Exit(code=1) from error

    slug: str = _mint_slug()
    resolved_title: str = title if title is not None else file.stem
    retrieved_at: str = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    payload: TextPayload = build_text_payload(body, slug=slug, title=resolved_title, retrieved_at=retrieved_at)
    signal_set: SignalSet = TextAdapter(agent=make_text_llm_agent(config), cache_dir=config.ingest_cache_dir).process(
        payload
    )

    signals_dir: Path = Path(tempfile.mkdtemp())
    signal_path: Path = signals_dir / f"{source_id_to_dirname(signal_set.source_ref.source_id)}.json"
    _ = signal_path.write_text(signal_set.model_dump_json(indent=_JSON_INDENT), encoding="utf-8")

    state: PipelineState = _run_signals_dir(signals_dir, config, through=through)
    _echo_outcome("Analysis complete.", state)


@app.command(name="run-latest")
def run_latest() -> None:
    """Detect the newest episode on the configured channel and run the pipeline if it is new (idempotent)."""
    config: Config = load_config()

    def read_latest(channel_id):
        return fetch_latest_video_id(channel_id, make_requests_http_get())

    def run_url(url):
        return _run_url(url, config, through=None)

    def now():
        return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

    ledger_path: Path = default_processed_episodes_path()

    try:
        result: RunLatestResult = run_latest_once(
            config, read_latest=read_latest, run_url=run_url, ledger_path=ledger_path, now=now
        )
    except SchedulerConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error

    if result.outcome is RunLatestOutcome.SKIPPED:
        typer.echo(f"No new episode; latest {result.source_id} already processed.")
    else:
        typer.echo(f"Processed {result.source_id} (run {result.slug}).")


if __name__ == "__main__":
    app()  # pragma: no cover
