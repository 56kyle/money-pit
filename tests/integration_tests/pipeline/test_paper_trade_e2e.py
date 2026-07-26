"""End-to-end integration test: paper-trade run with stub agents completes all expected nodes."""

import shutil
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel
from pydantic import TypeAdapter

from money_pit.compute.fills import build_fill_observation
from money_pit.constants import ACTION_STEPS_JSON_FILENAME
from money_pit.constants import ACTION_STEPS_VALIDATION_JSON_FILENAME
from money_pit.constants import DETERMINATION_JSON_FILENAME
from money_pit.constants import DETERMINATION_VERDICT_JSON_FILENAME
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.constants import UNDELIVERED_EMAIL_FILENAME_TEMPLATE
from money_pit.constants import VALIDATION_STATUS_FILENAME
from money_pit.email_sender import make_unconfigured_email_sender
from money_pit.graph.graph import EXECUTION_NODE
from money_pit.graph.state import PipelineState
from money_pit.mcp.manifest import pinned_manifest
from money_pit.schemas.fills import FillObservation
from money_pit.pipeline.orchestration import PipelineOverrides
from money_pit.pipeline.orchestration import phase4_overrides
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.determination import DeterminationReport
from money_pit.schemas.determination import DeterminationVerdict
from money_pit.schemas.enums import Determination
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.enums import RecoveryDecision
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.journal import ExecutionJournalEntry
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.recovery import PriorRunReconciliation
from money_pit.schemas.questions import InitialQuestions
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.validation_results import ActionStepsValidation
from money_pit.schemas.validation_results import ValidationStatusReport
from tests.conftest import UNCONFIGURED_EMAIL_REASON


ModelT = TypeVar("ModelT", bound=BaseModel)

_EXECUTE_PATH_STEPS: list[str] = [
    "recovery",
    "snapshot",
    "aggregator",
    "questions",
    "retrieval",
    "analysis",
    "validator",
    "determination",
    EXECUTION_NODE,
    "finalizer",
]

_PLAN_ONLY_PATH_STEPS: list[str] = [
    "recovery",
    "snapshot",
    "aggregator",
    "questions",
    "retrieval",
    "analysis",
    "validator",
    "determination",
]
"""Declared independently of _EXECUTE_PATH_STEPS so a node added before the pause fails this pin deliberately."""

_action_steps_adapter: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])


def _assert_file_valid(run_dir: Path, filename: str, model_class: type[ModelT]) -> ModelT:
    path = run_dir / filename
    assert path.exists(), f"Missing expected file: {filename}"
    return model_class.model_validate_json(path.read_text(encoding="utf-8"))


_EXECUTE_PATH_JSON_FILES: list[tuple[str, type[BaseModel]]] = [
    ("recovery.json", PriorRunReconciliation),
    ("portfolio_snapshot.json", PortfolioSnapshot),
    ("aggregated_signals.json", AggregatedSignals),
    ("initial_questions.json", InitialQuestions),
    ("initial_answers.json", InitialAnswers),
    ("action_steps_validation.json", ActionStepsValidation),
    ("execution_journal.json", ExecutionJournal),
]

_EXECUTE_PATH_MARKDOWN_FILES: list[str] = [
    "initial_questions.md",
    "initial_answers.md",
    "action_steps.md",
    "action_steps_validation.md",
]


@pytest.fixture(scope="module")
def execute_run(
    tmp_path_factory: pytest.TempPathFactory, pipeline_signals_dir: Path
) -> tuple[Path, PipelineState]:
    run_dir = tmp_path_factory.mktemp("execute_determination")
    overrides = phase4_overrides()
    overrides.manifest = pinned_manifest()
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
    assert report.sub_agent_spawned == EXECUTION_NODE


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


@pytest.mark.parametrize("step", _EXECUTE_PATH_STEPS)
def test_paper_trade_execute_path_completes_step(
    execute_run: tuple[Path, PipelineState], step: str
) -> None:
    _, final_state = execute_run
    assert step in final_state.get("completed_steps", [])


@pytest.mark.parametrize(("filename", "model_class"), _EXECUTE_PATH_JSON_FILES)
def test_paper_trade_execute_path_writes_valid_json(
    execute_run: tuple[Path, PipelineState], filename: str, model_class: type[BaseModel]
) -> None:
    run_dir, _ = execute_run
    _ = _assert_file_valid(run_dir, filename, model_class)


def test_paper_trade_execute_path_writes_action_steps(
    execute_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = execute_run
    action_steps = _action_steps_adapter.validate_json((run_dir / "action_steps.json").read_text(encoding="utf-8"))
    assert len(action_steps) >= 1


def test_paper_trade_execute_path_action_steps_all_succeed(
    execute_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = execute_run
    action_steps = _action_steps_adapter.validate_json((run_dir / "action_steps.json").read_text(encoding="utf-8"))
    assert all(s.step_failed is None for s in action_steps)


def test_paper_trade_execute_path_journal_entries_filled(
    execute_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = execute_run
    journal = _assert_file_valid(run_dir, "execution_journal.json", ExecutionJournal)
    assert all(entry.phase == ExecutionPhase.FILLED for entry in journal.entries)


def test_paper_trade_execute_path_journal_entries_populate_fills(
    execute_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = execute_run
    journal = _assert_file_valid(run_dir, "execution_journal.json", ExecutionJournal)
    assert all(
        (entry.filled_qty, entry.filled_avg_price, entry.realized_notional) == (1.0, 1.0, 1.0)
        for entry in journal.entries
    )


def test_paper_trade_execute_path_journal_outcome_clean(
    execute_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = execute_run
    journal = _assert_file_valid(run_dir, "execution_journal.json", ExecutionJournal)
    assert journal.outcome == ExecutionOutcome.EXECUTED_CLEAN


@pytest.mark.parametrize("md_filename", _EXECUTE_PATH_MARKDOWN_FILES)
def test_paper_trade_execute_path_renders_markdown(
    execute_run: tuple[Path, PipelineState], md_filename: str
) -> None:
    run_dir, _ = execute_run
    md_path = run_dir / md_filename
    assert md_path.exists()
    assert len(md_path.read_text(encoding="utf-8")) > 0


@pytest.fixture(scope="module")
def no_action_run(
    tmp_path_factory: pytest.TempPathFactory, pipeline_signals_dir: Path
) -> tuple[Path, PipelineState]:
    signals_dir = tmp_path_factory.mktemp("no_action_signals_in")
    _ = shutil.copy2(pipeline_signals_dir / "no_action_signal.json", signals_dir / "no_action_signal.json")
    run_dir = tmp_path_factory.mktemp("no_action_run")
    overrides = phase4_overrides()
    overrides.manifest = pinned_manifest()
    final_state = run_pipeline(signals_dir=signals_dir, run_dir=run_dir, overrides=overrides)
    return run_dir, final_state


def test_paper_trade_no_action_path_terminal_state_no_action(
    no_action_run: tuple[Path, PipelineState],
) -> None:
    _, final_state = no_action_run
    assert final_state.get("terminal_state") == TerminalState.NO_ACTION


def test_paper_trade_no_action_path_completes_terminal_step(
    no_action_run: tuple[Path, PipelineState],
) -> None:
    _, final_state = no_action_run
    assert "no_action_terminal" in (final_state.get("completed_steps") or [])


def test_paper_trade_no_action_path_skips_analysis(
    no_action_run: tuple[Path, PipelineState],
) -> None:
    _, final_state = no_action_run
    assert "analysis" not in (final_state.get("completed_steps") or [])


@pytest.mark.parametrize("filename", ["portfolio_snapshot.json", "aggregated_signals.json"])
def test_paper_trade_no_action_path_writes_pre_gate_file(
    no_action_run: tuple[Path, PipelineState], filename: str
) -> None:
    run_dir, _ = no_action_run
    assert (run_dir / filename).exists()


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
) -> tuple[Path, PipelineState]:
    run_dir = tmp_path / "run"
    overrides: PipelineOverrides = phase4_overrides()
    overrides.thesis_agent = _empty_thesis_agent
    overrides.send_email = post_processor_empty_email
    overrides.manifest = pinned_manifest()
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
) -> tuple[Path, PipelineState, _RecordingEmail]:
    run_dir = tmp_path_factory.mktemp("validation_error_determination")
    email = _RecordingEmail()
    overrides: PipelineOverrides = phase4_overrides()
    overrides.manifest = {}
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


def _incomplete_observer(client_order_id: str) -> FillObservation:
    """Return a terminal-but-non-clean fill (done_for_day, partial) so execution reaches EXECUTED_INCOMPLETE.

    done_for_day is a terminal status, so _poll_fill returns on the first observation with no
    sleep — the run stays fast despite the default 30s poll timeout. A positive filled_qty with a
    non-filled/non-rejected status maps to PARTIALLY_FILLED, which derives EXECUTED_INCOMPLETE.
    """
    _ = client_order_id
    return build_fill_observation("done_for_day", 3.0, 100.0)


@pytest.fixture(scope="module")
def execute_incomplete_run(
    tmp_path_factory: pytest.TempPathFactory, pipeline_signals_dir: Path
) -> tuple[Path, PipelineState, _RecordingEmail]:
    run_dir = tmp_path_factory.mktemp("execute_incomplete_determination")
    email = _RecordingEmail()
    overrides = phase4_overrides()
    overrides.observe_fill = _incomplete_observer
    overrides.send_email = email
    overrides.manifest = pinned_manifest()
    final_state = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides)
    return run_dir, final_state, email


def test_paper_trade_execute_incomplete_completes(
    execute_incomplete_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    _, final_state, _ = execute_incomplete_run
    assert "finalizer" in (final_state.get("completed_steps") or [])


def test_paper_trade_execute_incomplete_journal_outcome_incomplete(
    execute_incomplete_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    run_dir, _, _ = execute_incomplete_run
    journal = _assert_file_valid(run_dir, "execution_journal.json", ExecutionJournal)
    assert journal.outcome == ExecutionOutcome.EXECUTED_INCOMPLETE


def test_paper_trade_execute_incomplete_sends_notification(
    execute_incomplete_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    _, _, email = execute_incomplete_run
    assert any("Execution Incomplete" in subject for subject, _ in email.calls)


def test_paper_trade_execute_incomplete_determination_outcome_failure(
    execute_incomplete_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    run_dir, _, _ = execute_incomplete_run
    report = _assert_file_valid(run_dir, "determination.json", DeterminationReport)
    assert report.sub_agent_outcome == "failure"


def test_paper_trade_execute_clean_sends_no_notification(
    execute_run: tuple[Path, PipelineState],
) -> None:
    """Contrast: the clean execute path finalizes directly and never routes through notification."""
    _, final_state = execute_run
    assert "notification" not in (final_state.get("completed_steps") or [])


def test_paper_trade_fully_default_schema_builds_and_proceeds(tmp_path: Path, pipeline_signals_dir: Path) -> None:
    """A fully-default run now builds and reaches a PROCEED determination — the committed schema is pinned.

    make_validator_node resolves pinned_manifest() eagerly at graph construction against the
    default committed path, and the analysis post-processor emits execution params against that
    same pinned schema. Before the pin this raised ManifestUnavailableError at build; this test
    is the e2e regression guard that pinning the real schema un-breaks the default paper-trade run.
    """
    run_dir = tmp_path / "run"

    final_state = run_pipeline(
        signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=phase4_overrides()
    )

    assert final_state.get("terminal_state") is None
    report = _assert_file_valid(run_dir, "determination.json", DeterminationReport)
    assert report.determination == Determination.PROCEED


_PLAN_ONLY_PRESENT_ARTIFACTS: list[tuple[str, type[BaseModel]]] = [
    (ACTION_STEPS_VALIDATION_JSON_FILENAME, ActionStepsValidation),
    (VALIDATION_STATUS_FILENAME, ValidationStatusReport),
]


@pytest.fixture(scope="module")
def plan_only_run(
    tmp_path_factory: pytest.TempPathFactory, pipeline_signals_dir: Path
) -> tuple[Path, PipelineState]:
    run_dir = tmp_path_factory.mktemp("plan_only_determination")
    overrides = phase4_overrides()
    overrides.manifest = pinned_manifest()
    final_state = run_pipeline(
        signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides, stop_before_execution=True
    )
    return run_dir, final_state


def test_paper_trade_plan_only_completes_planning_steps_only(
    plan_only_run: tuple[Path, PipelineState],
) -> None:
    """The run pauses at the execution interrupt, completing exactly the independently-declared planning steps."""
    _, final_state = plan_only_run
    assert final_state.get("completed_steps") == _PLAN_ONLY_PATH_STEPS


def test_paper_trade_plan_only_terminal_state_none(
    plan_only_run: tuple[Path, PipelineState],
) -> None:
    _, final_state = plan_only_run
    assert final_state.get("terminal_state") is None


def test_paper_trade_plan_only_determination_proceeds(
    plan_only_run: tuple[Path, PipelineState],
) -> None:
    """The paused state still carries PROCEED — the run is mid-flight, not abandoned."""
    _, final_state = plan_only_run
    assert final_state.get("determination") == Determination.PROCEED


def test_paper_trade_plan_only_writes_no_execution_journal(
    plan_only_run: tuple[Path, PipelineState],
) -> None:
    """The capital-critical pin: no execution journal means no order was ever placed.

    The in-memory state does carry determination PROCEED and sub_agent_spawned == EXECUTION_NODE because
    the run is genuinely paused at the interrupt; absence of this file is what proves nothing executed.
    """
    run_dir, _ = plan_only_run
    assert not (run_dir / EXECUTION_JOURNAL_FILENAME).exists()


def test_paper_trade_plan_only_writes_no_determination(
    plan_only_run: tuple[Path, PipelineState],
) -> None:
    """determination.json is the finalizer's record; its absence proves the run was never concluded."""
    run_dir, _ = plan_only_run
    assert not (run_dir / DETERMINATION_JSON_FILENAME).exists()


def test_paper_trade_plan_only_writes_verdict(
    plan_only_run: tuple[Path, PipelineState],
) -> None:
    """Paired with the absent determination.json, the verdict artifact distinguishes paused from concluded.

    determination_verdict.json present means the gate decided; determination.json absent means the
    run never concluded — exactly the artifact pairing an ADR 0035 plan-only run leaves on disk.
    """
    run_dir, _ = plan_only_run
    verdict = _assert_file_valid(run_dir, DETERMINATION_VERDICT_JSON_FILENAME, DeterminationVerdict)
    assert verdict.determination == Determination.PROCEED


@pytest.mark.parametrize(("filename", "model_class"), _PLAN_ONLY_PRESENT_ARTIFACTS)
def test_paper_trade_plan_only_writes_valid_planning_artifact(
    plan_only_run: tuple[Path, PipelineState], filename: str, model_class: type[BaseModel]
) -> None:
    run_dir, _ = plan_only_run
    _ = _assert_file_valid(run_dir, filename, model_class)


def test_paper_trade_plan_only_writes_action_steps(
    plan_only_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = plan_only_run
    action_steps = _action_steps_adapter.validate_json(
        (run_dir / ACTION_STEPS_JSON_FILENAME).read_text(encoding="utf-8")
    )
    assert len(action_steps) >= 1


_RECOVERY_PRIOR_SLUG: str = "2000-01-01"
_RECOVERY_PRIOR_CLIENT_ORDER_ID: str = f"{_RECOVERY_PRIOR_SLUG}:s1"


class _RecoveryScenarioObserver:
    """A real FillObserver shared by recovery re-observation and the current run's execution.

    The prior run's leg is scripted by its exact client_order_id; every other order (the current
    run's own legs) observes filled. client_order_ids are `{slug}:{step_id}`, so the far-past
    prior slug keeps the prior leg distinguishable from the current run's date-slugged orders.
    """

    def __init__(self, prior_observation: FillObservation) -> None:
        self._prior_observation = prior_observation
        self.calls: list[str] = []

    def __call__(self, client_order_id: str) -> FillObservation:
        self.calls.append(client_order_id)
        if client_order_id == _RECOVERY_PRIOR_CLIENT_ORDER_ID:
            return self._prior_observation
        return build_fill_observation("filled", 1.0, 1.0)


def _write_prior_journal(daily_show_root: Path, phase: ExecutionPhase) -> None:
    """Author a prior EXECUTED_INCOMPLETE run journal with one potentially-open leg under daily_show_root."""
    run_dir = daily_show_root / _RECOVERY_PRIOR_SLUG
    run_dir.mkdir(parents=True, exist_ok=True)
    entry = ExecutionJournalEntry(
        step_id="s1",
        group_id=None,
        client_order_id=_RECOVERY_PRIOR_CLIENT_ORDER_ID,
        phase=phase,
        intended={},
        broker_order_id="broker-s1",
        status=None,
        filled_qty=None,
        filled_avg_price=None,
        realized_notional=None,
        compensation_of=None,
        error=None,
        timestamp="2000-01-01T00:00:00Z",
    )
    journal = ExecutionJournal(slug=_RECOVERY_PRIOR_SLUG, outcome=ExecutionOutcome.EXECUTED_INCOMPLETE, entries=[entry])
    (run_dir / EXECUTION_JOURNAL_FILENAME).write_text(journal.model_dump_json(), encoding="utf-8")


@pytest.fixture
def recovery_halt_run(
    tmp_path: Path, pipeline_signals_dir: Path
) -> tuple[Path, PipelineState, _RecordingEmail]:
    daily_show_root = tmp_path
    _write_prior_journal(daily_show_root, ExecutionPhase.SUBMITTED)
    run_dir = daily_show_root / "current_run"
    email = _RecordingEmail()
    overrides = phase4_overrides()
    overrides.observe_fill = _RecoveryScenarioObserver(build_fill_observation("accepted", None, None))
    overrides.send_email = email
    overrides.manifest = pinned_manifest()
    final_state = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides)
    return run_dir, final_state, email


def test_paper_trade_recovery_halt_writes_halt_decision(
    recovery_halt_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    run_dir, _, _ = recovery_halt_run
    record = _assert_file_valid(run_dir, "recovery.json", PriorRunReconciliation)
    assert record.decision is RecoveryDecision.HALT


def test_paper_trade_recovery_halt_state_decision(
    recovery_halt_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    _, final_state, _ = recovery_halt_run
    assert final_state.get("recovery_decision") is RecoveryDecision.HALT


def test_paper_trade_recovery_halt_sends_halt_email(
    recovery_halt_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    _, _, email = recovery_halt_run
    assert any("Recovery Halt" in subject for subject, _ in email.calls)


@pytest.mark.parametrize("artifact", ["portfolio_snapshot.json", "action_steps.json", "determination.json"])
def test_paper_trade_recovery_halt_writes_no_planning_artifacts(
    recovery_halt_run: tuple[Path, PipelineState, _RecordingEmail], artifact: str
) -> None:
    run_dir, _, _ = recovery_halt_run
    assert not (run_dir / artifact).exists()


@pytest.fixture
def undeliverable_halt_run(tmp_path: Path, pipeline_signals_dir: Path) -> tuple[Path, PipelineState]:
    """A recovery-HALT run whose injected sender always raises EmailSendError, pinning run_pipeline's own wrapping.

    send_email is handed in raw — the undelivered-record decorator is applied by run_pipeline itself, so
    the artifact appearing in run_dir is the only evidence that the single composition point still wraps.
    """
    daily_show_root = tmp_path
    _write_prior_journal(daily_show_root, ExecutionPhase.SUBMITTED)
    run_dir = daily_show_root / "current_run"
    overrides = phase4_overrides()
    overrides.observe_fill = _RecoveryScenarioObserver(build_fill_observation("accepted", None, None))
    overrides.send_email = make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON)
    overrides.manifest = pinned_manifest()
    final_state = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides)
    return run_dir, final_state


def test_paper_trade_undeliverable_halt_email_still_halts(
    undeliverable_halt_run: tuple[Path, PipelineState],
) -> None:
    _, final_state = undeliverable_halt_run
    assert final_state.get("recovery_decision") is RecoveryDecision.HALT


def test_paper_trade_undeliverable_halt_email_writes_halt_record(
    undeliverable_halt_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = undeliverable_halt_run
    record = _assert_file_valid(run_dir, "recovery.json", PriorRunReconciliation)
    assert record.decision is RecoveryDecision.HALT


def test_paper_trade_undeliverable_halt_email_records_artifact(
    undeliverable_halt_run: tuple[Path, PipelineState],
) -> None:
    run_dir, _ = undeliverable_halt_run
    artifact = run_dir / UNDELIVERED_EMAIL_FILENAME_TEMPLATE.format(index=1)
    assert artifact.is_file()
    assert "money-pit: Recovery Halt - " in artifact.read_text(encoding="utf-8")


@pytest.fixture
def recovery_notice_run(
    tmp_path: Path, pipeline_signals_dir: Path
) -> tuple[Path, PipelineState, _RecordingEmail]:
    daily_show_root = tmp_path
    _write_prior_journal(daily_show_root, ExecutionPhase.SUBMITTED)
    run_dir = daily_show_root / "current_run"
    email = _RecordingEmail()
    overrides = phase4_overrides()
    overrides.observe_fill = _RecoveryScenarioObserver(build_fill_observation("filled", 8.0, 100.0))
    overrides.send_email = email
    overrides.manifest = pinned_manifest()
    final_state = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=run_dir, overrides=overrides)
    return run_dir, final_state, email


def test_paper_trade_recovery_notice_writes_notice_decision(
    recovery_notice_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    run_dir, _, _ = recovery_notice_run
    record = _assert_file_valid(run_dir, "recovery.json", PriorRunReconciliation)
    assert record.decision is RecoveryDecision.PROCEED_WITH_NOTICE


def test_paper_trade_recovery_notice_sends_notice_email(
    recovery_notice_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    _, _, email = recovery_notice_run
    assert any("Prior Run Reconciled" in subject for subject, _ in email.calls)


def test_paper_trade_recovery_notice_proceeds_to_execution(
    recovery_notice_run: tuple[Path, PipelineState, _RecordingEmail],
) -> None:
    run_dir, final_state, _ = recovery_notice_run
    assert "finalizer" in (final_state.get("completed_steps") or [])
    report = _assert_file_valid(run_dir, "determination.json", DeterminationReport)
    assert report.determination == Determination.PROCEED
