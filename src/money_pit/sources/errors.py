"""Module containing typed source-layer failures for the money_pit package."""


class SourceError(Exception):
    """Base class for source registry and connector failures."""


class SourceRegistryError(SourceError):
    """Base class for invalid source registry documents."""


class SourceRegistryReadError(SourceRegistryError):
    """Raised when a source registry cannot be read or decoded."""


class UnsupportedSourceRegistryVersionError(SourceRegistryError):
    """Raised when a registry uses an unsupported schema version."""


class DuplicateSourceIdError(SourceRegistryError):
    """Raised when a registry defines one source identifier more than once."""


class UnknownAdapterError(SourceRegistryError):
    """Raised when a source refers to an adapter that is not registered."""


class AdapterAlreadyRegisteredError(SourceRegistryError):
    """Raised when an adapter name is registered more than once."""


class ConnectorConfigurationError(SourceError):
    """Raised when connector-specific configuration is invalid."""


class SourceDiscoveryError(SourceError):
    """Raised when bounded source discovery fails."""


class UnsupportedDirectIngestionError(SourceError):
    """Raised when a configured connector cannot ingest a caller-provided URL."""


class InvalidYouTubeVideoUrlError(SourceError):
    """Raised when direct ingestion receives an invalid YouTube video URL."""


class SourceFetchError(SourceError):
    """Raised when source content cannot be fetched."""


class YouTubeSourceMembershipError(SourceFetchError):
    """Raised when acquired video metadata does not match its configured source."""


class SourceContentTooLargeError(SourceFetchError):
    """Raised before source content can exceed its configured byte limit."""


class SourceMediaTypeError(SourceFetchError):
    """Raised when fetched content has an unsupported media type."""


class SourceExtractionError(SourceError):
    """Raised when fetched content cannot be safely extracted."""
