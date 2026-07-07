"""Module containing prior-journal recovery that reads execution_journal.json and reconciles state before planning begins for the money_pit package."""

from pathlib import Path

from money_pit.schemas.journal import ExecutionJournal


def recover_prior_state(working_dir: Path) -> ExecutionJournal | None:
    """Read a prior execution_journal.json and reconcile state before planning.

    Deferred to Phase 8; not wired into any graph or pipeline path yet. Activating
    this module must be a deliberate act, so it fails loudly rather than silently
    reporting a clean slate.
    """
    _ = working_dir
    raise NotImplementedError(
        "Prior-journal recovery is deferred to Phase 8; execution_journal.json "
        "reconciliation is not implemented."
    )
