"""Module containing run-repository failures for the money_pit package."""


class RunRepositoryError(Exception):
    """Base class for generic run-repository failures."""


class RunAlreadyExistsError(RunRepositoryError):
    """Raised when an existing run manifest differs from the requested record."""


class RunManifestWriteError(RunRepositoryError):
    """Raised when a run manifest cannot be written atomically."""


class RunRegistrationIncompleteError(RunRepositoryError):
    """Raised when a recoverable cross-store run registration remains pending."""

    def __init__(self, run_id: str, phase: str) -> None:
        """Identify the run and incomplete registration phase."""
        self.run_id: str = run_id
        self.phase: str = phase
        super().__init__(f"Run {run_id} registration is incomplete at {phase}.")


class RunReconciliationNotFoundError(RunRepositoryError):
    """Raised when no database, final, or pending record exists for a run ID."""
