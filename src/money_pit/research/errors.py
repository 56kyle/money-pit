"""Module containing typed research failures."""


class ResearchError(Exception):
    """Base class for bounded research failures."""


class ResearchProviderAlreadyRegisteredError(ResearchError):
    """Raised when a provider name is registered more than once."""


class ResearchProviderNotFoundError(ResearchError):
    """Raised when a task requests a provider outside the registry."""


class ResearchProviderMismatchError(ResearchError):
    """Raised when a provider receives another provider's query or result."""


class ResearchSearchError(ResearchError):
    """Raised when a provider cannot complete a bounded search."""


class ResearchFetchError(ResearchError):
    """Raised when a discovery result cannot become a fetched artifact."""


class ResearchBudgetExceededError(ResearchError):
    """Raised before a round could exceed a durable session budget."""


class HistoricalResearchUnavailableError(ResearchError):
    """Raised before current-only provider I/O during an explicit historical run."""


class ResearchEvidenceCutoffError(ResearchError):
    """Raised when provider metadata cannot prove evidence existed by the cutoff."""


class ResearchUriReuseMismatchError(ResearchError):
    """Raised when a job-owned canonical URI no longer matches its reuse policy."""


class ResearchSessionStoppedError(ResearchError):
    """Raised when new work is attempted for a non-active session."""


class ResearchSessionOwnershipError(ResearchError):
    """Raised when an interrupted session is still owned by a live run."""


class ResearchStageRecoveryIncompleteError(ResearchError):
    """Raised when staged A3 work exists but is not terminal and recoverable."""


class ResearchStageAlreadyAdmittedError(ResearchError):
    """Raised when recovery is requested after A3 was already admitted."""


class ResearchIdentityCollisionError(ResearchError, ValueError):
    """Base class for reused durable research identities."""


class ResearchTaskCollisionError(ResearchIdentityCollisionError):
    """Raised when a task identity refers to different work."""


class ResearchResultCollisionError(ResearchIdentityCollisionError):
    """Raised when a result identity refers to different discovery metadata."""


class ResearchFetchCollisionError(ResearchIdentityCollisionError):
    """Raised when a fetch identity refers to a different outcome."""
