"""Tests for money_pit.pipeline.notification subject/body conventions.

Pins the contract-§7 / architecture-§6.9 subject convention `money-pit: <reason> - {slug}`.
The VALIDATION_ERROR reason is a pinned external contract (`money-pit: MCP Validation Error -
{slug}`), so its exact text is asserted; the ANALYSIS_HALT reason wording is left free — only the
convention (prefix + `- {slug}` suffix) is pinned. Wave D adds the execution-incomplete reason
(`money-pit: Execution Incomplete - {slug}`), reached with terminal_state None and
execution_outcome EXECUTED_INCOMPLETE, whose body carries the per-entry execution-journal summary.
"""

from pathlib import Path

import pytest

from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.constants import UNDELIVERED_EMAIL_FILENAME_TEMPLATE
from money_pit.email_sender import make_unconfigured_email_sender
from money_pit.email_sender import with_undelivered_record
from money_pit.graph.state import PipelineState
from money_pit.pipeline.notification import _build_subject
from money_pit.pipeline.notification import make_notification_node
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.journal import ExecutionJournalEntry
from tests.conftest import UNCONFIGURED_EMAIL_REASON


_SLUG = "test-run"


class _RecordingEmail:
    """Injected send_email stub that records every (subject, body) call for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, subject: str, body: str) -> None:
        self.calls.append((subject, body))


def _journal_entry(
    step_id: str,
    phase: ExecutionPhase,
    status: str | None,
    filled_qty: float | None,
) -> ExecutionJournalEntry:
    return ExecutionJournalEntry(
        step_id=step_id,
        group_id=None,
        client_order_id=f"COID-{step_id}",
        phase=phase,
        intended={"symbol": step_id},
        broker_order_id=f"BROKER-{step_id}",
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=None,
        realized_notional=None,
        compensation_of=None,
        error=None,
        timestamp="2026-07-13T00:00:00+00:00",
    )


def test__build_subject_with_validation_error() -> None:
    subject = _build_subject(_SLUG, TerminalState.VALIDATION_ERROR, None, None)

    assert subject == f"money-pit: MCP Validation Error - {_SLUG}"


def test__build_subject_with_analysis_halt_follows_convention() -> None:
    subject = _build_subject(_SLUG, TerminalState.ANALYSIS_HALT, None, None)

    assert subject.startswith("money-pit: ")
    assert subject.endswith(f" - {_SLUG}")


def test__build_subject_with_execution_incomplete() -> None:
    subject = _build_subject(_SLUG, None, ExecutionOutcome.EXECUTED_INCOMPLETE, None)

    assert subject == f"money-pit: Execution Incomplete - {_SLUG}"


def test__build_subject_with_unexpected_combo_raises() -> None:
    with pytest.raises(ValueError):
        _build_subject(_SLUG, None, None, None)


@pytest.fixture
def incomplete_journal_entries() -> list[ExecutionJournalEntry]:
    return [
        _journal_entry("step-1", ExecutionPhase.FILLED, "filled", 100.0),
        _journal_entry("step-2", ExecutionPhase.PARTIALLY_FILLED, "done_for_day", 40.0),
        _journal_entry("step-3", ExecutionPhase.SUBMITTED, "accepted", None),
    ]


@pytest.fixture
def incomplete_working_dir(
    tmp_path: Path, incomplete_journal_entries: list[ExecutionJournalEntry]
) -> Path:
    journal = ExecutionJournal(
        slug=_SLUG,
        outcome=ExecutionOutcome.EXECUTED_INCOMPLETE,
        entries=incomplete_journal_entries,
    )
    _ = (tmp_path / EXECUTION_JOURNAL_FILENAME).write_text(
        journal.model_dump_json(indent=2), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def execution_incomplete_email(incomplete_working_dir: Path) -> _RecordingEmail:
    email = _RecordingEmail()
    node = make_notification_node(send_email=email)
    state: PipelineState = {
        "slug": _SLUG,
        "working_dir": str(incomplete_working_dir),
        "execution_outcome": ExecutionOutcome.EXECUTED_INCOMPLETE,
    }
    _ = node(state)
    return email


def test_notification_node_with_execution_incomplete_sends_subject(
    execution_incomplete_email: _RecordingEmail,
) -> None:
    subject, _ = execution_incomplete_email.calls[0]
    assert subject == f"money-pit: Execution Incomplete - {_SLUG}"


@pytest.fixture
def undeliverable_notification_result(incomplete_working_dir: Path) -> PipelineState:
    send_email = with_undelivered_record(
        make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON), incomplete_working_dir
    )
    node = make_notification_node(send_email=send_email)
    state: PipelineState = {
        "slug": _SLUG,
        "working_dir": str(incomplete_working_dir),
        "execution_outcome": ExecutionOutcome.EXECUTED_INCOMPLETE,
        "completed_steps": [],
    }
    return node(state)


def test_notification_node_with_undeliverable_email_still_completes_step(
    undeliverable_notification_result: PipelineState,
) -> None:
    assert "notification" in (undeliverable_notification_result.get("completed_steps") or [])


@pytest.fixture
def undeliverable_notification_working_dir(
    undeliverable_notification_result: PipelineState, incomplete_working_dir: Path
) -> Path:
    """The working dir after the notification node has run against an undeliverable sender."""
    return incomplete_working_dir


def test_notification_node_with_undeliverable_email_records_artifact(
    undeliverable_notification_working_dir: Path,
) -> None:
    recorded = (undeliverable_notification_working_dir / UNDELIVERED_EMAIL_FILENAME_TEMPLATE.format(index=1)).read_text(
        encoding="utf-8"
    )

    assert f"money-pit: Execution Incomplete - {_SLUG}" in recorded


def test_notification_node_with_execution_incomplete_body_summarizes_entries(
    execution_incomplete_email: _RecordingEmail,
    incomplete_journal_entries: list[ExecutionJournalEntry],
) -> None:
    _, body = execution_incomplete_email.calls[0]
    for entry in incomplete_journal_entries:
        expected_line = (
            f"  {entry.step_id} | {entry.phase.value} | {entry.status} | filled_qty={entry.filled_qty}"
        )
        assert expected_line in body
