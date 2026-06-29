"""End-to-end integration test: paper-trade run with stub agents completes all expected nodes."""
import shutil
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, TypeAdapter

from money_pit.pipeline.orchestration import run_pipeline
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.questions import InitialQuestions
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.validation_results import ActionStepsValidation

ModelT = TypeVar("ModelT", bound=BaseModel)

_SIGNALS_DIR: Path = Path(__file__).parent / "fixtures" / "signals"

_EXECUTE_PATH_STEPS: list[str] = [
    "snapshot",
    "aggregator",
    "questions",
    "retrieval",
    "analysis",
    "validator",
    "execution",
]

_action_steps_adapter: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])


def _assert_file_valid(run_dir: Path, filename: str, model_class: type[ModelT]) -> ModelT:
    path = run_dir / filename
    assert path.exists(), f"Missing expected file: {filename}"
    return model_class.model_validate_json(path.read_text(encoding="utf-8"))


def test_paper_trade_execute_path(tmp_path: Path) -> None:
    """Full pipeline run with stub agents completes via the execute branch."""
    run_dir = tmp_path / "run"

    final_state = run_pipeline(signals_dir=_SIGNALS_DIR, run_dir=run_dir)

    completed = final_state.get("completed_steps", [])
    for step in _EXECUTE_PATH_STEPS:
        assert step in completed, f"Missing completed step: {step}"

    _ = _assert_file_valid(run_dir, "portfolio_snapshot.json", PortfolioSnapshot)
    _ = _assert_file_valid(run_dir, "aggregated_signals.json", AggregatedSignals)
    _ = _assert_file_valid(run_dir, "initial_questions.json", InitialQuestions)
    _ = _assert_file_valid(run_dir, "initial_answers.json", InitialAnswers)
    _ = _assert_file_valid(run_dir, "action_steps_validation.json", ActionStepsValidation)
    _ = _assert_file_valid(run_dir, "execution_journal.json", ExecutionJournal)

    action_steps = _action_steps_adapter.validate_json(
        (run_dir / "action_steps.json").read_text(encoding="utf-8")
    )
    assert len(action_steps) >= 1, "Expected at least one action step on the execute path"
    assert all(s.step_failed is None for s in action_steps), "All action steps must have step_failed=None"

    for md_filename in ("initial_questions.md", "initial_answers.md", "action_steps.md", "action_steps_validation.md"):
        md_path = run_dir / md_filename
        assert md_path.exists(), f"Missing markdown file: {md_filename}"
        assert len(md_path.read_text(encoding="utf-8")) > 0, f"Empty markdown file: {md_filename}"

    assert final_state.get("terminal_state") is None, "Execute path must not set terminal_state"


def test_paper_trade_no_action_path(tmp_path: Path) -> None:
    """Pipeline sets NO_ACTION terminal state when signals are not actionable."""
    no_action_signals = Path(__file__).parent / "fixtures" / "signals" / "no_action_signal.json"
    signals_dir = tmp_path / "signals_in"
    signals_dir.mkdir()
    _ = shutil.copy2(no_action_signals, signals_dir / "no_action_signal.json")
    run_dir = tmp_path / "run"

    final_state = run_pipeline(signals_dir=signals_dir, run_dir=run_dir)

    assert final_state.get("terminal_state") == TerminalState.NO_ACTION
    assert "no_action_terminal" in (final_state.get("completed_steps") or [])
    # Questions/retrieval/analysis nodes should NOT have run
    assert "analysis" not in (final_state.get("completed_steps") or [])
    # Portfolio snapshot and aggregated signals are still written (run up to signal_gate)
    assert (run_dir / "portfolio_snapshot.json").exists()
    assert (run_dir / "aggregated_signals.json").exists()
