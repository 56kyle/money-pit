"""End-to-end integration test: paper-trade run with stub agents completes all expected nodes."""

import shutil
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel
from pydantic import TypeAdapter

from money_pit.graph.state import PipelineState
from money_pit.mcp.manifest import ManifestUnavailableError
from money_pit.mcp.manifest import pinned_manifest
from money_pit.mcp.order_schema import AlpacaOrderSchemaNotPinnedError
from money_pit.pipeline.orchestration import PipelineOverrides
from money_pit.pipeline.orchestration import phase4_overrides
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.determination import DeterminationReport
from money_pit.schemas.enums import Determination
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.questions import InitialQuestions
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.validation_results import ActionStepsValidation


ModelT = TypeVar("ModelT", bound=BaseModel)

_EXECUTE_PATH_STEPS: list[str] = [
    "snapshot",
    "aggregator",
    "questions",
    "retrieval",
    "analysis",
    "validator",
    "determination",
    "execution",
    "finalizer",
]

_action_steps_adapter: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])


def _assert_file_valid(run_dir: Path, filename: str, model_class: type[ModelT]) -> ModelT:
    path = run_dir / filename
    assert path.exists(), f"Missing expected file: {filename}"
    return model_class.model_validate_json(path.read_text(encoding="utf-8"))


def test_paper_trade_execute_path(
    tmp_path: Path, pipeline_signals_dir: Path, stub_free_order_schema_path: Path
) -> None:
    """Full pipeline run with stub agents completes via the execute branch."""
    run_dir = tmp_path / "run"

    overrides = phase4_overrides()
    overrides.order_schema_path = stub_free_order_schema_path
    overrides.manifest = pinned_manifest(stub_free_order_schema_path)
    final_state = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides)

    completed = final_state.get("completed_steps", [])
    for step in _EXECUTE_PATH_STEPS:
        assert step in completed, f"Missing completed step: {step}"

    _ = _assert_file_valid(run_dir, "portfolio_snapshot.json", PortfolioSnapshot)
    _ = _assert_file_valid(run_dir, "aggregated_signals.json", AggregatedSignals)
    _ = _assert_file_valid(run_dir, "initial_questions.json", InitialQuestions)
    _ = _assert_file_valid(run_dir, "initial_answers.json", InitialAnswers)
    _ = _assert_file_valid(run_dir, "action_steps_validation.json", ActionStepsValidation)
    _ = _assert_file_valid(run_dir, "execution_journal.json", ExecutionJournal)

    action_steps = _action_steps_adapter.validate_json((run_dir / "action_steps.json").read_text(encoding="utf-8"))
    assert len(action_steps) >= 1, "Expected at least one action step on the execute path"
    assert all(s.step_failed is None for s in action_steps), "All action steps must have step_failed=None"

    for md_filename in ("initial_questions.md", "initial_answers.md", "action_steps.md", "action_steps_validation.md"):
        md_path = run_dir / md_filename
        assert md_path.exists(), f"Missing markdown file: {md_filename}"
        assert len(md_path.read_text(encoding="utf-8")) > 0, f"Empty markdown file: {md_filename}"

    assert final_state.get("terminal_state") is None, "Execute path must not set terminal_state"


def test_paper_trade_no_action_path(
    tmp_path: Path, pipeline_signals_dir: Path, stub_free_order_schema_path: Path
) -> None:
    """Pipeline sets NO_ACTION terminal state when signals are not actionable."""
    no_action_signals = pipeline_signals_dir / "no_action_signal.json"
    signals_dir = tmp_path / "signals_in"
    signals_dir.mkdir()
    _ = shutil.copy2(no_action_signals, signals_dir / "no_action_signal.json")
    run_dir = tmp_path / "run"

    overrides = phase4_overrides()
    overrides.manifest = pinned_manifest(stub_free_order_schema_path)
    final_state = run_pipeline(signals_dir=signals_dir, run_dir=run_dir, overrides=overrides)

    assert final_state.get("terminal_state") == TerminalState.NO_ACTION
    assert "no_action_terminal" in (final_state.get("completed_steps") or [])
    # Questions/retrieval/analysis nodes should NOT have run
    assert "analysis" not in (final_state.get("completed_steps") or [])
    # Portfolio snapshot and aggregated signals are still written (run up to signal_gate)
    assert (run_dir / "portfolio_snapshot.json").exists()
    assert (run_dir / "aggregated_signals.json").exists()


@pytest.fixture(scope="module")
def execute_run(
    tmp_path_factory: pytest.TempPathFactory, pipeline_signals_dir: Path, stub_free_order_schema_path: Path
) -> tuple[Path, PipelineState]:
    run_dir = tmp_path_factory.mktemp("execute_determination")
    overrides = phase4_overrides()
    overrides.order_schema_path = stub_free_order_schema_path
    overrides.manifest = pinned_manifest(stub_free_order_schema_path)
    final_state = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides)
    return run_dir, final_state


def test_paper_trade_execute_path_writes_determination_proceed(
    execute_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = execute_run
    report = _assert_file_valid(run_dir, "determination.json", DeterminationReport)
    assert report.determination == Determination.PROCEED


def test_paper_trade_execute_path_determination_spawns_execution(
    execute_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = execute_run
    report = _assert_file_valid(run_dir, "determination.json", DeterminationReport)
    assert report.sub_agent_spawned == "execution"


def test_paper_trade_execute_path_determination_outcome_success(
    execute_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = execute_run
    report = _assert_file_valid(run_dir, "determination.json", DeterminationReport)
    assert report.sub_agent_outcome == "success"


def test_paper_trade_execute_path_terminal_state_none(
    execute_run: tuple[Path, PipelineState],
) -> None:
    _, final_state = execute_run
    assert final_state.get("terminal_state") is None


class _RecordingEmail:
    """Injected send_email stub that records every call so a test can assert it was never invoked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, subject: str, body: str) -> None:
        self.calls.append((subject, body))


def _empty_thesis_agent(*_args: object, **_kwargs: object) -> AnalysisJudgment:
    """Return a judgment with no theses and no halt — drives the post-processor-empty NO_ACTION path."""
    return AnalysisJudgment(theses=[], dropped_claims=[], macro_read=[], halt=None)


@pytest.fixture
def post_processor_empty_email() -> _RecordingEmail:
    return _RecordingEmail()


@pytest.fixture
def post_processor_empty_run(
    tmp_path: Path,
    pipeline_signals_dir: Path,
    post_processor_empty_email: _RecordingEmail,
    stub_free_order_schema_path: Path,
) -> tuple[Path, PipelineState]:
    run_dir = tmp_path / "run"
    overrides: PipelineOverrides = phase4_overrides()
    overrides.thesis_agent = _empty_thesis_agent
    overrides.send_email = post_processor_empty_email
    overrides.manifest = pinned_manifest(stub_free_order_schema_path)
    final_state = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides)
    return run_dir, final_state


def test_paper_trade_post_processor_empty_terminal_state_no_action(
    post_processor_empty_run: tuple[Path, PipelineState],
) -> None:
    _, final_state = post_processor_empty_run
    assert final_state.get("terminal_state") == TerminalState.NO_ACTION


def test_paper_trade_post_processor_empty_sends_no_email(
    post_processor_empty_run: tuple[Path, PipelineState],
    post_processor_empty_email: _RecordingEmail,
) -> None:
    assert post_processor_empty_email.calls == []


def test_paper_trade_post_processor_empty_writes_no_determination(
    post_processor_empty_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = post_processor_empty_run
    assert not (run_dir / "determination.json").exists()


@pytest.fixture(scope="module")
def validation_error_run(
    tmp_path_factory: pytest.TempPathFactory,
    pipeline_signals_dir: Path,
    stub_free_order_schema_path: Path,
) -> tuple[Path, PipelineState, _RecordingEmail]:
    run_dir = tmp_path_factory.mktemp("validation_error_determination")
    email = _RecordingEmail()
    overrides: PipelineOverrides = phase4_overrides()
    overrides.manifest = {}
    overrides.order_schema_path = stub_free_order_schema_path
    overrides.send_email = email
    final_state = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides)
    return run_dir, final_state, email


def test_paper_trade_validation_error_writes_determination_halt(
    validation_error_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    run_dir, _, _ = validation_error_run
    report = _assert_file_valid(run_dir, "determination.json", DeterminationReport)
    assert report.determination == Determination.HALT


def test_paper_trade_validation_error_spawns_notification(
    validation_error_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    run_dir, _, _ = validation_error_run
    report = _assert_file_valid(run_dir, "determination.json", DeterminationReport)
    assert report.sub_agent_spawned == "notification"


def test_paper_trade_validation_error_has_failed_steps(
    validation_error_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    run_dir, _, _ = validation_error_run
    report = _assert_file_valid(run_dir, "determination.json", DeterminationReport)
    assert report.failed_steps


def test_paper_trade_validation_error_sends_email(
    validation_error_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    _, _, email = validation_error_run
    assert email.calls


def test_paper_trade_fully_default_schema_fails_closed_at_build(tmp_path: Path, pipeline_signals_dir: Path) -> None:
    """A fully-default run cannot even build its graph while the committed schema is the unpinned stub.

    make_validator_node resolves pinned_manifest() eagerly at graph construction, so a
    run_pipeline with no manifest override loudly raises ManifestUnavailableError (wrapping
    the NotPinned sentinel error) before any node executes — the earliest fail-closed gate.
    """
    run_dir = tmp_path / "run"

    with pytest.raises(ManifestUnavailableError):
        _ = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=phase4_overrides())


def test_paper_trade_unpinned_order_schema_fails_closed_at_analysis(
    tmp_path: Path, pipeline_signals_dir: Path, stub_free_order_schema_path: Path
) -> None:
    """With a pinned manifest but no order_schema_path opt-in, the analysis node fails closed.

    The graph builds (manifest injected), then the analysis post-processor emits execution
    params against the default committed stub; load_order_schema raises NotPinned and it
    propagates uncaught out of run_pipeline. This pins the specific re-enforced gate: a real
    unpinned production run cannot silently proceed to materialize orders.
    """
    run_dir = tmp_path / "run"
    overrides = phase4_overrides()
    overrides.manifest = pinned_manifest(stub_free_order_schema_path)

    with pytest.raises(AlpacaOrderSchemaNotPinnedError):
        _ = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides)
