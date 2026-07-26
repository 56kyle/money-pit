"""Test cases for the __main__ module — every command's argument handling, exit codes and operator-facing output.

pin-order-schema's live seams (credential resolution, MCP introspection) are monkeypatched at their
__main__ import sites, and the committed schema path is redirected to a tmp file so a test never overwrites
the real alpaca_order_schema.json. Assertions target CLI exit codes and the written file's shape.

`--through` is pinned by capturing it in the _run_signals_dir spy rather than by any artifact, because a bound
that is dropped between the CLI and run_pipeline has no other trace: the run simply executes. `run` and
`analyze-text` are each pinned over every Stage plus the omitted case, and the one link the spy stands in for
— _run_signals_dir handing `through` on to run_pipeline — is pinned separately, so no layer is unwitnessed.

The `stage` command runs its real node: `determination` needs no credentials (ADR 0036), so only load_config
is stubbed. Its two refusals are pinned by exit code and by the artifact each message must name on stderr,
and an unknown stage name is pinned as typer's own usage error rather than a hand-rolled check.
"""

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import SecretStr
from pytest import MonkeyPatch
from typer.testing import CliRunner

from money_pit import __main__
from money_pit.adapters.text_llm import TextPayload
from money_pit.adapters.video_llm import TranscriptSource
from money_pit.adapters.video_llm import VideoPayload
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.constants import ACTION_STEPS_VALIDATION_JSON_FILENAME
from money_pit.constants import DETERMINATION_VERDICT_JSON_FILENAME
from money_pit.constants import SIGNALS_DIRNAME
from money_pit.graph.state import PipelineState
from money_pit.ingestion.artifacts import Keyframe
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.ingestion.artifacts import VideoArtifacts
from money_pit.ingestion.keyframes import _seconds_to_locator
from money_pit.ingestion.pipeline import IngestionSeams
from money_pit.ingestion.pipeline import _source_id_from_url
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.orchestration import PipelineOverrides
from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import OverallValidationStatus
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import SourceType
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signal_draft import ClaimDraft
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.schemas.signals import SignalSet
from money_pit.schemas.validation_results import ActionStepsValidation
from money_pit.schemas.validation_results import ValidationStep


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
    return AlpacaCredentials(api_key="the-key", secret_key=SecretStr("the-secret"), paper=True)


@pytest.fixture
def stub_credential_resolution(monkeypatch: MonkeyPatch, credentials: AlpacaCredentials) -> None:
    monkeypatch.setattr(__main__, "load_config", lambda: None)
    monkeypatch.setattr(__main__, "resolve_alpaca_credentials", lambda _config: credentials)


def test_main_succeeds(runner: CliRunner) -> None:
    """It exits with a status code of zero."""
    result = runner.invoke(__main__.app, ["money-pit"])
    assert result.exit_code == 0


def test_run_latest_command_registered() -> None:
    from money_pit.__main__ import app

    assert any(command.name == "run-latest" for command in app.registered_commands)


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


_INGEST_URL: str = "https://youtu.be/dQw4w9WgXcQ"
_INGEST_SOURCE_ID: str = "yt:dQw4w9WgXcQ"
_INGEST_SANITIZED_NAME: str = "yt_dQw4w9WgXcQ.json"
_INGEST_SLUG: str = "2026-06-18_14-30-00"
_INGEST_MAX_FRAMES: int = 40
_HIGH_CLAIM_ID: str = "claim_001"
_LOW_CLAIM_ID: str = "claim_002"


def _ingest_source_ref(url: str) -> SourceRef:
    return SourceRef(
        source_id=_source_id_from_url(url),
        source_type=SourceType.NARRATED_VIDEO,
        title="T",
        url=url,
        published_at="2026-06-18T00:00:00Z",
        retrieved_at="2026-06-18T14:30:00Z",
        locator=None,
    )


def _make_ingest_seams(uploader_caption_path: Path) -> IngestionSeams:
    """Build an all-fake IngestionSeams whose uploader captions win so no transcriber/network runs."""

    def fake_downloader(url: str, out_dir: Path) -> VideoArtifacts:
        return VideoArtifacts(
            source_ref=_ingest_source_ref(url),
            audio_path=None,
            video_path=Path("v.mp4"),
            uploader_caption_path=uploader_caption_path,
            auto_caption_path=None,
            metadata_path=out_dir / "metadata.json",
        )

    def fake_transcriber(audio: Path) -> TranscriptResult:
        _ = audio
        return TranscriptResult(text="whisper transcript", source=TranscriptSource.WHISPER, has_word_timestamps=True)

    def fake_detector(video: Path) -> list[float]:
        _ = video
        return [1.0, 5.0]

    def fake_extractor(video: Path, timestamps: list[float], out_dir: Path) -> list[Keyframe]:
        _ = video
        return [
            Keyframe(timestamp=timestamp, image_path=out_dir / f"kf_{index}.png", locator=_seconds_to_locator(timestamp))
            for index, timestamp in enumerate(timestamps)
        ]

    def fake_on_screen(keyframes: list[Keyframe]) -> list[OnScreenExtraction]:
        return [
            OnScreenExtraction(locator=keyframe.locator, on_screen_text=["NVDA chart"], cited_sources=["Bloomberg"])
            for keyframe in keyframes
        ]

    return IngestionSeams(
        downloader=fake_downloader,
        transcriber=fake_transcriber,
        detector=fake_detector,
        extractor=fake_extractor,
        on_screen=fake_on_screen,
    )


def _ingest_draft(source_ref: SourceRef) -> SignalSetDraft:
    return SignalSetDraft(
        source_id=source_ref.source_id,
        source_type=source_ref.source_type.value,
        title=source_ref.title,
        url=source_ref.url,
        published_at=source_ref.published_at,
        retrieved_at=source_ref.retrieved_at,
        summary="NVDA is well-positioned for AI infrastructure. Risk is AMD competition.",
        claims=[
            ClaimDraft(
                claim_id=_HIGH_CLAIM_ID,
                claim="NVDA data center segment shows 200%+ YoY growth driven by AI infrastructure buildout.",
                tier=SignalTier.HIGH.value,
                category=ClaimCategory.FUNDAMENTAL.value,
                tickers_affected=["NVDA"],
                cited_sources=[],
            ),
            ClaimDraft(
                claim_id=_LOW_CLAIM_ID,
                claim="AMD competition is increasing in the GPU space.",
                tier=SignalTier.LOW.value,
                category=ClaimCategory.SENTIMENT.value,
                tickers_affected=["AMD"],
                cited_sources=[],
            ),
        ],
        tickers_mentioned=["NVDA", "AMD"],
        sectors_mentioned=["Technology", "Semiconductors"],
        macro_themes=["AI infrastructure buildout"],
    )


@pytest.fixture
def uploader_caption_path(data_folder: Path) -> Path:
    return data_folder / "adapters" / "video" / "uploader.en.vtt"


@pytest.fixture
def ingest_seams(uploader_caption_path: Path) -> IngestionSeams:
    return _make_ingest_seams(uploader_caption_path)


@pytest.fixture
def ingest_agent() -> Callable[[VideoPayload], SignalSetDraft]:
    def _agent(payload: VideoPayload) -> SignalSetDraft:
        return _ingest_draft(payload.source_ref)

    return _agent


@pytest.fixture
def signals_dir(tmp_path: Path) -> Path:
    return tmp_path / "signals"


@pytest.fixture
def signal_file(
    ingest_seams: IngestionSeams,
    ingest_agent: Callable[[VideoPayload], SignalSetDraft],
    signals_dir: Path,
    tmp_path: Path,
) -> Path:
    return __main__._ingest_to_signal_file(
        url=_INGEST_URL,
        slug=_INGEST_SLUG,
        cache_dir=tmp_path / "cache",
        signals_dir=signals_dir,
        seams=ingest_seams,
        agent=ingest_agent,
        max_frames=_INGEST_MAX_FRAMES,
    )


def test__ingest_to_signal_file_returns_existing_path(signal_file: Path) -> None:
    assert signal_file.exists()


def test__ingest_to_signal_file_uses_sanitized_filename(signal_file: Path) -> None:
    assert signal_file.name == _INGEST_SANITIZED_NAME


def test__ingest_to_signal_file_preserves_logical_source_id(signal_file: Path) -> None:
    signal_set = SignalSet.model_validate_json(signal_file.read_text(encoding="utf-8"))
    assert signal_set.source_ref.source_id == _INGEST_SOURCE_ID


def test__ingest_to_signal_file_carries_agent_claims(signal_file: Path) -> None:
    signal_set = SignalSet.model_validate_json(signal_file.read_text(encoding="utf-8"))
    assert [claim.claim_id for claim in signal_set.claims] == [_HIGH_CLAIM_ID, _LOW_CLAIM_ID]


def test__ingest_to_signal_file_writes_only_the_one_file(signal_file: Path, signals_dir: Path) -> None:
    assert signals_dir.is_dir()
    assert list(signals_dir.iterdir()) == [signal_file]


_ANALYZE_TEXT_BODY: str = "NVDA is well-positioned for AI infrastructure buildout. Risk: AMD competition."
_ANALYZE_TEXT_TERMINAL_STATE: TerminalState = TerminalState.NO_ACTION
_SPY_RUN_DIR_NAME: str = "2026-07-26_09-00-00"


def _analyze_text_draft(payload: TextPayload) -> SignalSetDraft:
    source_ref: SourceRef = payload.source_ref
    return SignalSetDraft(
        source_id=source_ref.source_id,
        source_type=source_ref.source_type.value,
        title=source_ref.title,
        url=source_ref.url,
        published_at=source_ref.published_at,
        retrieved_at=source_ref.retrieved_at,
        summary="NVDA thesis.",
        claims=[
            ClaimDraft(
                claim_id=_HIGH_CLAIM_ID,
                claim="NVDA data center segment shows 200%+ YoY growth.",
                tier=SignalTier.HIGH.value,
                category=ClaimCategory.FUNDAMENTAL.value,
                tickers_affected=["NVDA"],
                cited_sources=[],
            ),
        ],
        tickers_mentioned=["NVDA"],
        sectors_mentioned=["Technology"],
        macro_themes=["AI infrastructure buildout"],
    )


@dataclass(frozen=True)
class _RunSignalsDirCall:
    """One recorded invocation of _run_signals_dir, holding both the arguments a CLI command chooses."""

    signals_dir: Path
    through: Stage | None


@dataclass
class _RunSignalsDirSpy:
    """Records each call to the stubbed _run_signals_dir and returns a fixed terminal-state PipelineState.

    `through` is captured rather than discarded because a `--through` dropped anywhere between the CLI and
    run_pipeline turns an operator's plan-only request into a real execution, with no other observable trace.
    """

    calls: list[_RunSignalsDirCall]

    def __call__(self, signals_dir: Path, _config: Config, *, through: Stage | None) -> PipelineState:
        self.calls.append(_RunSignalsDirCall(signals_dir=signals_dir, through=through))
        return PipelineState(
            terminal_state=_ANALYZE_TEXT_TERMINAL_STATE, working_dir=str(signals_dir / _SPY_RUN_DIR_NAME)
        )


@pytest.fixture
def run_signals_dir_spy() -> _RunSignalsDirSpy:
    return _RunSignalsDirSpy(calls=[])


@dataclass
class _AgentSpy:
    """Records whether the text LLM agent factory was invoked, standing in for make_text_llm_agent."""

    called: bool

    def __call__(self, _config: Config) -> Callable[[TextPayload], SignalSetDraft]:
        self.called = True
        return _analyze_text_draft


@pytest.fixture
def make_text_llm_agent_spy() -> _AgentSpy:
    return _AgentSpy(called=False)


@pytest.fixture
def cli_config(tmp_path: Path) -> Config:
    return Config(
        alpaca_service="the-service",
        alpaca_username="the-user",
        alpaca_paper=True,
        ingest_cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def stub_analyze_text(
    monkeypatch: MonkeyPatch,
    cli_config: Config,
    run_signals_dir_spy: _RunSignalsDirSpy,
    make_text_llm_agent_spy: _AgentSpy,
) -> None:
    monkeypatch.setattr(__main__, "load_config", lambda: cli_config)
    monkeypatch.setattr(__main__, "make_text_llm_agent", make_text_llm_agent_spy)
    monkeypatch.setattr(__main__, "_run_signals_dir", run_signals_dir_spy)


@pytest.fixture
def thesis_file(tmp_path: Path) -> Path:
    path = tmp_path / "thesis.txt"
    _ = path.write_text(_ANALYZE_TEXT_BODY, encoding="utf-8")
    return path


def test_analyze_text_command_registered() -> None:
    assert any(command.name == "analyze-text" for command in __main__.app.registered_commands)


def test_analyze_text_with_valid_exits_zero(
    runner: CliRunner, stub_analyze_text: None, thesis_file: Path
) -> None:
    result = runner.invoke(__main__.app, ["analyze-text", str(thesis_file)])

    assert result.exit_code == 0


def test_analyze_text_with_valid_runs_signals_dir_once(
    runner: CliRunner, stub_analyze_text: None, thesis_file: Path, run_signals_dir_spy: _RunSignalsDirSpy
) -> None:
    _ = runner.invoke(__main__.app, ["analyze-text", str(thesis_file)])

    assert len(run_signals_dir_spy.calls) == 1


def test_analyze_text_with_valid_writes_one_signal_file(
    runner: CliRunner, stub_analyze_text: None, thesis_file: Path, run_signals_dir_spy: _RunSignalsDirSpy
) -> None:
    _ = runner.invoke(__main__.app, ["analyze-text", str(thesis_file)])

    signals_dir = run_signals_dir_spy.calls[0].signals_dir
    signal_files = list(signals_dir.iterdir())
    assert len(signal_files) == 1
    _ = SignalSet.model_validate_json(signal_files[0].read_text(encoding="utf-8"))


def test_analyze_text_with_valid_echoes_terminal_state(
    runner: CliRunner, stub_analyze_text: None, thesis_file: Path
) -> None:
    result = runner.invoke(__main__.app, ["analyze-text", str(thesis_file)])

    assert _ANALYZE_TEXT_TERMINAL_STATE.value in result.output


@pytest.fixture
def empty_thesis_file(tmp_path: Path) -> Path:
    """A file holding only whitespace, which read_thesis refuses as an empty thesis."""
    path: Path = tmp_path / "empty.txt"
    _ = path.write_text("   \n\t\n", encoding="utf-8")
    return path


def test_analyze_text_with_missing_path_exits_one(
    runner: CliRunner, stub_analyze_text: None, tmp_path: Path
) -> None:
    result = runner.invoke(__main__.app, ["analyze-text", str(tmp_path / "missing.txt")])

    assert result.exit_code == 1


def test_analyze_text_with_missing_path_refuses_before_the_llm(
    runner: CliRunner,
    stub_analyze_text: None,
    tmp_path: Path,
    run_signals_dir_spy: _RunSignalsDirSpy,
    make_text_llm_agent_spy: _AgentSpy,
) -> None:
    """An unreadable thesis must cost nothing: no LLM call is billed and no pipeline run begins."""
    _ = runner.invoke(__main__.app, ["analyze-text", str(tmp_path / "missing.txt")])

    assert make_text_llm_agent_spy.called is False
    assert run_signals_dir_spy.calls == []


def test_analyze_text_with_empty_file_exits_one(
    runner: CliRunner, stub_analyze_text: None, empty_thesis_file: Path
) -> None:
    result = runner.invoke(__main__.app, ["analyze-text", str(empty_thesis_file)])

    assert result.exit_code == 1


def test_analyze_text_with_empty_file_refuses_before_the_llm(
    runner: CliRunner,
    stub_analyze_text: None,
    empty_thesis_file: Path,
    run_signals_dir_spy: _RunSignalsDirSpy,
    make_text_llm_agent_spy: _AgentSpy,
) -> None:
    _ = runner.invoke(__main__.app, ["analyze-text", str(empty_thesis_file)])

    assert make_text_llm_agent_spy.called is False
    assert run_signals_dir_spy.calls == []


_EVERY_STAGE: list[Stage] = list(Stage)
_USAGE_ERROR_EXIT_CODE: int = 2
_STAGE_FAILURE_EXIT_CODE: int = 1
_TRACEBACK_MARKER: str = "Traceback"


@pytest.mark.parametrize("through", [pytest.param(stage, id=stage.value) for stage in _EVERY_STAGE])
def test_analyze_text_with_through_forwards_the_stage(
    runner: CliRunner,
    stub_analyze_text: None,
    thesis_file: Path,
    run_signals_dir_spy: _RunSignalsDirSpy,
    through: Stage,
) -> None:
    """The operator's bound must survive to the pipeline call; dropped, a plan-only run becomes a real execution."""
    result = runner.invoke(__main__.app, ["analyze-text", str(thesis_file), "--through", through.value])

    assert result.exit_code == 0
    assert run_signals_dir_spy.calls[0].through is through


def test_analyze_text_without_through_forwards_none(
    runner: CliRunner, stub_analyze_text: None, thesis_file: Path, run_signals_dir_spy: _RunSignalsDirSpy
) -> None:
    """An unbounded run is the default, and it is expressed as None rather than as any stage."""
    _ = runner.invoke(__main__.app, ["analyze-text", str(thesis_file)])

    assert run_signals_dir_spy.calls[0].through is None


def test_analyze_text_with_valid_echoes_the_run_directory(
    runner: CliRunner, stub_analyze_text: None, thesis_file: Path
) -> None:
    """A plain full run still tells the operator where the artifacts landed, not only a bounded one."""
    result = runner.invoke(__main__.app, ["analyze-text", str(thesis_file)])

    assert _SPY_RUN_DIR_NAME in result.output


@dataclass
class _VideoAgentSpy:
    """Stands in for make_video_llm_agent, handing back the same fake draft the ingestion fixtures use."""

    called: bool

    def __call__(self, _config: Config) -> Callable[[VideoPayload], SignalSetDraft]:
        self.called = True
        return lambda payload: _ingest_draft(payload.source_ref)


@pytest.fixture
def make_video_llm_agent_spy() -> _VideoAgentSpy:
    return _VideoAgentSpy(called=False)


@pytest.fixture
def stub_run(
    monkeypatch: MonkeyPatch,
    cli_config: Config,
    ingest_seams: IngestionSeams,
    make_video_llm_agent_spy: _VideoAgentSpy,
    run_signals_dir_spy: _RunSignalsDirSpy,
) -> None:
    """Replace only `run`'s outward-facing seams, so ingestion and the command's own argument handling run for real."""
    monkeypatch.setattr(__main__, "load_config", lambda: cli_config)
    monkeypatch.setattr(__main__, "production_seams", lambda _config: ingest_seams)
    monkeypatch.setattr(__main__, "make_video_llm_agent", make_video_llm_agent_spy)
    monkeypatch.setattr(__main__, "_run_signals_dir", run_signals_dir_spy)


@pytest.mark.parametrize("through", [pytest.param(stage, id=stage.value) for stage in _EVERY_STAGE])
def test_run_with_through_forwards_the_stage(
    runner: CliRunner, stub_run: None, run_signals_dir_spy: _RunSignalsDirSpy, through: Stage
) -> None:
    """`run` reaches the pipeline through _run_url, so the bound crosses one more layer than analyze-text's."""
    result = runner.invoke(__main__.app, ["run", _INGEST_URL, "--through", through.value])

    assert result.exit_code == 0
    assert run_signals_dir_spy.calls[0].through is through


def test_run_without_through_forwards_none(
    runner: CliRunner, stub_run: None, run_signals_dir_spy: _RunSignalsDirSpy
) -> None:
    _ = runner.invoke(__main__.app, ["run", _INGEST_URL])

    assert run_signals_dir_spy.calls[0].through is None


def test_run_with_valid_exits_zero(runner: CliRunner, stub_run: None) -> None:
    result = runner.invoke(__main__.app, ["run", _INGEST_URL])

    assert result.exit_code == 0


def test_run_with_valid_echoes_the_run_directory(runner: CliRunner, stub_run: None) -> None:
    result = runner.invoke(__main__.app, ["run", _INGEST_URL])

    assert _SPY_RUN_DIR_NAME in result.output


def test_run_with_unknown_through_is_rejected_by_typer(
    runner: CliRunner, stub_run: None, run_signals_dir_spy: _RunSignalsDirSpy
) -> None:
    """`--through execution` is the plausible operator mistake, and it must be a usage error rather than a run.

    `execution` is not a Stage (ADR 0036), so typer refuses it before anything is ingested or any pipeline
    starts — the alternative, silently treating an unrecognized bound as no bound, is a real execution.
    """
    result = runner.invoke(__main__.app, ["run", _INGEST_URL, "--through", "execution"])

    assert result.exit_code == _USAGE_ERROR_EXIT_CODE
    assert run_signals_dir_spy.calls == []


@pytest.fixture
def ephemeral_signals_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the throwaway directory _run_url mints, so invoking it leaves nothing behind outside tmp_path.

    _run_url reaches `tempfile.mkdtemp` directly rather than through an injected seam, so the redirection has
    to be applied to that function itself.
    """
    root: Path = tmp_path / "ephemeral-signals"
    root.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda: str(root))
    return root


@pytest.mark.parametrize(
    "through",
    [pytest.param(None, id="none"), *[pytest.param(stage, id=stage.value) for stage in _EVERY_STAGE]],
)
def test__run_url_forwards_through_to_run_signals_dir(
    stub_run: None,
    ephemeral_signals_root: Path,
    run_signals_dir_spy: _RunSignalsDirSpy,
    cli_config: Config,
    through: Stage | None,
) -> None:
    """`through` is keyword-only with no default here, and two signature changes have already passed through it.

    The `run` command's tests reach this function only incidentally, so the bound is pinned against a direct
    call as well: a `through` dropped between _run_url and _run_signals_dir turns a plan-only request into a
    real execution with no other observable trace.
    """
    _ = __main__._run_url(_INGEST_URL, cli_config, through=through)

    assert run_signals_dir_spy.calls[0].through is through


def test__run_url_runs_the_directory_it_ingested_into(
    stub_run: None,
    ephemeral_signals_root: Path,
    run_signals_dir_spy: _RunSignalsDirSpy,
    cli_config: Config,
) -> None:
    """The ingest and the run must name the same ephemeral directory, else the pipeline runs over nothing."""
    _ = __main__._run_url(_INGEST_URL, cli_config, through=None)

    run_dir: Path = run_signals_dir_spy.calls[0].signals_dir
    assert run_dir == ephemeral_signals_root
    assert [path.name for path in run_dir.iterdir()] == [_INGEST_SANITIZED_NAME]


@dataclass
class _RunPipelineSpy:
    """Records the `through` keyword _run_signals_dir hands to run_pipeline."""

    calls: list[Stage | None]

    def __call__(self, _signals_dir: Path, *, overrides: PipelineOverrides, through: Stage | None) -> PipelineState:
        _ = overrides
        self.calls.append(through)
        return PipelineState(terminal_state=_ANALYZE_TEXT_TERMINAL_STATE)


@pytest.fixture
def run_pipeline_spy() -> _RunPipelineSpy:
    return _RunPipelineSpy(calls=[])


@pytest.fixture
def stub_run_pipeline(monkeypatch: MonkeyPatch, run_pipeline_spy: _RunPipelineSpy) -> None:
    """production_deps is stubbed too, since composing the real one needs credentials this test has no use for."""
    monkeypatch.setattr(__main__, "production_deps", lambda _config: PipelineOverrides())
    monkeypatch.setattr(__main__, "run_pipeline", run_pipeline_spy)


@pytest.mark.parametrize(
    "through",
    [pytest.param(None, id="none"), *[pytest.param(stage, id=stage.value) for stage in _EVERY_STAGE]],
)
def test__run_signals_dir_forwards_through_to_run_pipeline(
    stub_run_pipeline: None,
    run_pipeline_spy: _RunPipelineSpy,
    cli_config: Config,
    tmp_path: Path,
    through: Stage | None,
) -> None:
    """The last link of the chain the CLI tests stub out: the bound reaches run_pipeline itself, as a keyword."""
    _ = __main__._run_signals_dir(tmp_path / SIGNALS_DIRNAME, cli_config, through=through)

    assert run_pipeline_spy.calls == [through]


_STAGE_SLUG: str = "2026-07-26_09-00-00"


@pytest.fixture
def stage_run_dir(tmp_path: Path) -> Path:
    """An existing run directory named the way run_pipeline names one, since run_stage reads the slug from it."""
    path: Path = tmp_path / _STAGE_SLUG
    path.mkdir()
    return path


@pytest.fixture
def stage_prerequisite(stage_run_dir: Path) -> Path:
    """Write determination's one input, so the success path is a real node run rather than a stubbed one."""
    validation: ActionStepsValidation = ActionStepsValidation(
        slug=stage_run_dir.name,
        overall_status=OverallValidationStatus.VALIDATED,
        steps=[
            ValidationStep(
                step_id="step-1",
                status=ValidationStatus.MATCHED,
                tool_sequence=None,
                compensation_sequence=None,
                gap_description=None,
            )
        ],
    )
    path: Path = stage_run_dir / ACTION_STEPS_VALIDATION_JSON_FILENAME
    _ = path.write_text(validation.model_dump_json(indent=2), encoding="utf-8")
    return path


@pytest.fixture
def stub_stage(monkeypatch: MonkeyPatch, cli_config: Config) -> None:
    """Only load_config is stubbed: determination needs no credentials, so the real node runs (ADR 0036)."""
    monkeypatch.setattr(__main__, "load_config", lambda: cli_config)


def test_stage_with_valid_exits_zero(
    runner: CliRunner, stub_stage: None, stage_run_dir: Path, stage_prerequisite: Path
) -> None:
    result = runner.invoke(__main__.app, ["stage", Stage.DETERMINATION.value, "--run-dir", str(stage_run_dir)])

    assert result.exit_code == 0


def test_stage_with_valid_echoes_the_run_directory(
    runner: CliRunner, stub_stage: None, stage_run_dir: Path, stage_prerequisite: Path
) -> None:
    """A standalone stage writes back into a directory the operator supplied, so it is echoed back to them."""
    result = runner.invoke(__main__.app, ["stage", Stage.DETERMINATION.value, "--run-dir", str(stage_run_dir)])

    assert str(stage_run_dir) in result.output


def test_stage_with_valid_runs_the_node(
    runner: CliRunner, stub_stage: None, stage_run_dir: Path, stage_prerequisite: Path
) -> None:
    """The control for the refusal tests: the same command against a satisfied run directory leaves a verdict."""
    _ = runner.invoke(__main__.app, ["stage", Stage.DETERMINATION.value, "--run-dir", str(stage_run_dir)])

    assert (stage_run_dir / DETERMINATION_VERDICT_JSON_FILENAME).is_file()


def test_stage_with_absent_run_dir_exits_non_zero(runner: CliRunner, stub_stage: None, tmp_path: Path) -> None:
    result = runner.invoke(
        __main__.app, ["stage", Stage.DETERMINATION.value, "--run-dir", str(tmp_path / "never-created")]
    )

    assert result.exit_code == _STAGE_FAILURE_EXIT_CODE


def test_stage_with_absent_run_dir_names_the_directory_on_stderr(
    runner: CliRunner, stub_stage: None, tmp_path: Path
) -> None:
    """StageRunDirectoryError reaches the operator as a message on stderr, not as a traceback out of the CLI."""
    missing: Path = tmp_path / "never-created"

    result = runner.invoke(__main__.app, ["stage", Stage.DETERMINATION.value, "--run-dir", str(missing)])

    assert str(missing) in result.stderr
    assert _TRACEBACK_MARKER not in result.stderr


def test_stage_with_missing_prerequisite_exits_non_zero(
    runner: CliRunner, stub_stage: None, stage_run_dir: Path
) -> None:
    result = runner.invoke(__main__.app, ["stage", Stage.DETERMINATION.value, "--run-dir", str(stage_run_dir)])

    assert result.exit_code == _STAGE_FAILURE_EXIT_CODE


def test_stage_with_missing_prerequisite_names_the_artifact_on_stderr(
    runner: CliRunner, stub_stage: None, stage_run_dir: Path
) -> None:
    """StagePrerequisiteError must name the artifact an operator has to produce, not merely report a failure."""
    result = runner.invoke(__main__.app, ["stage", Stage.DETERMINATION.value, "--run-dir", str(stage_run_dir)])

    assert ACTION_STEPS_VALIDATION_JSON_FILENAME in result.stderr
    assert _TRACEBACK_MARKER not in result.stderr


def test_stage_with_missing_prerequisite_leaves_the_run_directory_untouched(
    runner: CliRunner, stub_stage: None, stage_run_dir: Path
) -> None:
    """The refusal precedes the node, evidenced by the verdict the determination node writes on every path."""
    _ = runner.invoke(__main__.app, ["stage", Stage.DETERMINATION.value, "--run-dir", str(stage_run_dir)])

    assert list(stage_run_dir.iterdir()) == []


def test_stage_with_unknown_name_is_rejected_by_typer(
    runner: CliRunner, stub_stage: None, stage_run_dir: Path
) -> None:
    """`execution` is not a Stage (ADR 0036), and typer's own enum parsing is what refuses it — a usage error."""
    result = runner.invoke(__main__.app, ["stage", "execution", "--run-dir", str(stage_run_dir)])

    assert result.exit_code == _USAGE_ERROR_EXIT_CODE
    assert list(stage_run_dir.iterdir()) == []
