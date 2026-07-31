"""Module containing durable-storage failures for the money_pit package."""


class StorageError(Exception):
    """Base class for durable-storage failures."""


class StorageCapabilityError(StorageError):
    """Raised when the SQLite runtime lacks a required capability."""


class StorageConnectionError(StorageError):
    """Raised when a SQLite connection cannot be opened or configured."""


class StorageTransactionError(StorageError):
    """Raised when a transaction cannot begin, commit, or roll back."""


class MigrationError(StorageError):
    """Base class for schema migration failures."""


class MigrationDiscoveryError(MigrationError):
    """Raised when packaged migration resources are malformed."""


class MigrationHistoryError(MigrationError):
    """Raised when database history is not a prefix of packaged migrations."""


class MigrationChecksumError(MigrationHistoryError):
    """Raised when an applied migration differs from its packaged resource."""


class MigrationApplyError(MigrationError):
    """Raised when a packaged migration cannot be applied."""


class AssetStoreError(StorageError):
    """Base class for content-addressed asset failures."""


class AssetWriteError(AssetStoreError):
    """Raised when an asset cannot be persisted atomically."""


class AssetIntegrityError(AssetStoreError):
    """Raised when an existing asset does not match its digest."""
