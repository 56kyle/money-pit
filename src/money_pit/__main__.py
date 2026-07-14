"""Command-line interface."""

import datetime
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from money_pit.adapters.video import VideoAdapter
from money_pit.adapters.video_llm import VideoPayload
from money_pit.adapters.video_llm import make_video_llm_agent
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.config import load_config
from money_pit.config import resolve_alpaca_credentials
from money_pit.constants import FILE_SAFE_DATETIME_FORMAT
from money_pit.constants import SIGNALS_DIRNAME
from money_pit.constants import source_id_to_dirname
from money_pit.graph.state import PipelineState
from money_pit.ingestion.pipeline import IngestionSeams
from money_pit.ingestion.pipeline import ingest_video
from money_pit.ingestion.pipeline import production_seams
from money_pit.log import configure_file_logging
from money_pit.mcp.clients import list_write_tools
from money_pit.mcp.constants import PLACE_STOCK_ORDER_TOOL
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_PATH
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL
from money_pit.pipeline.orchestration import production_deps
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.schemas.signals import SignalSet


if TYPE_CHECKING:
    from mcp.types import Tool


app: typer.Typer = typer.Typer()

_JSON_INDENT: int = 2
_NO_TERMINAL_STATE_LABEL: str = "none"


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


@app.command()
def run(url: str) -> None:
    """Ingest a URL into a fresh ephemeral signals directory and execute the full pipeline over it."""
    config: Config = load_config()
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
    state: PipelineState = run_pipeline(signals_dir, overrides=production_deps(config))
    terminal_state: TerminalState | None = state.get("terminal_state")
    label: str = terminal_state.value if terminal_state is not None else _NO_TERMINAL_STATE_LABEL
    typer.echo(f"Run complete. Terminal state: {label}")


if __name__ == "__main__":
    app()  # pragma: no cover
