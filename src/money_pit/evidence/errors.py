"""Module containing typed evidence processing failures."""


class EvidenceProcessingError(Exception):
    """Base class for evidence processing failures."""


class EvidenceProcessorAlreadyRegisteredError(EvidenceProcessingError):
    """Raised when a processor name is registered more than once."""


class EvidenceProcessorNotFoundError(EvidenceProcessingError):
    """Raised when no processor supports an acquisition media type."""


class EvidenceProjectionError(EvidenceProcessingError):
    """Raised when evidence cannot fit within a bounded projection."""


class UnknownEvidenceAliasError(EvidenceProcessingError):
    """Raised when model output refers to an alias it was not shown."""
