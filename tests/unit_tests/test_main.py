"""Test cases for the __main__ module — the money-pit no-op command and the pin-order-schema introspector.

pin-order-schema's live seams (credential resolution, MCP introspection) are monkeypatched at their
__main__ import sites, and the committed schema path is redirected to a tmp file so a test never overwrites
the real alpaca_order_schema.json. Assertions target CLI exit codes and the written file's shape.
"""

import json
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
from money_pit.graph.state import PipelineState
from money_pit.ingestion.artifacts import Keyframe
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.ingestion.artifacts import VideoArtifacts
from money_pit.ingestion.keyframes import _seconds_to_locator
from money_pit.ingestion.pipeline import IngestionSeams
from money_pit.ingestion.pipeline import _source_id_from_url
from money_pit.mcp.order_schema import ALPACA_ORDER_SCHEMA_STUB_SENTINEL
from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import SourceType
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signal_draft import ClaimDraft
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.schemas.signals import SignalSet


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


def test__run_url_is_importable() -> None:
    from money_pit.__main__ import _run_url

    assert callable(_run_url)


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


@dataclass
class _RunSignalsDirSpy:
    """Records each call to the stubbed _run_signals_dir and returns a fixed terminal-state PipelineState."""

    calls: list[Path]

    def __call__(self, signals_dir: Path, _config: Config) -> PipelineState:
        self.calls.append(signals_dir)
        return PipelineState(terminal_state=_ANALYZE_TEXT_TERMINAL_STATE)


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
def analyze_text_config(tmp_path: Path) -> Config:
    return Config(
        alpaca_service="the-service",
        alpaca_username="the-user",
        alpaca_paper=True,
        ingest_cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def stub_analyze_text(
    monkeypatch: MonkeyPatch,
    analyze_text_config: Config,
    run_signals_dir_spy: _RunSignalsDirSpy,
    make_text_llm_agent_spy: _AgentSpy,
) -> None:
    monkeypatch.setattr(__main__, "load_config", lambda: analyze_text_config)
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

    signals_dir = run_signals_dir_spy.calls[0]
    signal_files = list(signals_dir.iterdir())
    assert len(signal_files) == 1
    _ = SignalSet.model_validate_json(signal_files[0].read_text(encoding="utf-8"))


def test_analyze_text_with_valid_echoes_terminal_state(
    runner: CliRunner, stub_analyze_text: None, thesis_file: Path
) -> None:
    result = runner.invoke(__main__.app, ["analyze-text", str(thesis_file)])

    assert _ANALYZE_TEXT_TERMINAL_STATE.value in result.output


def test_analyze_text_with_missing_path_exits_one_before_llm(
    runner: CliRunner,
    stub_analyze_text: None,
    tmp_path: Path,
    run_signals_dir_spy: _RunSignalsDirSpy,
    make_text_llm_agent_spy: _AgentSpy,
) -> None:
    result = runner.invoke(__main__.app, ["analyze-text", str(tmp_path / "missing.txt")])

    assert result.exit_code == 1
    assert make_text_llm_agent_spy.called is False
    assert run_signals_dir_spy.calls == []


def test_analyze_text_with_empty_file_exits_one_before_llm(
    runner: CliRunner,
    stub_analyze_text: None,
    tmp_path: Path,
    run_signals_dir_spy: _RunSignalsDirSpy,
    make_text_llm_agent_spy: _AgentSpy,
) -> None:
    empty_path = tmp_path / "empty.txt"
    _ = empty_path.write_text("   \n\t\n", encoding="utf-8")

    result = runner.invoke(__main__.app, ["analyze-text", str(empty_path)])

    assert result.exit_code == 1
    assert make_text_llm_agent_spy.called is False
    assert run_signals_dir_spy.calls == []
