"""Module containing run-repository failures for the money_pit package."""


class RunRepositoryError(Exception):
    """Base class for generic run-repository failures."""


class RunAlreadyExistsError(RunRepositoryError):
    """Raised when a run directory or manifest already exists."""


class RunManifestWriteError(RunRepositoryError):
    """Raised when a run manifest cannot be written atomically."""
