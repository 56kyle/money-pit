"""Module containing durable-storage failures for the money_pit package."""


class StorageError(Exception):
    """Base class for durable-storage failures."""


class StorageCapabilityError(StorageError):
    """Raised when the SQLite runtime lacks a required capability."""


class StorageConnectionError(StorageError):
    """Raised when a SQLite connection cannot be opened or configured."""


class StorageTransactionError(StorageError):
    """Raised when a transaction cannot begin, commit, or roll back."""


class SchemaIdentityError(StorageError):
    """Base class for schema baseline and identity failures."""


class BaselineDiscoveryError(SchemaIdentityError):
    """Raised when the packaged release baseline is malformed."""


class UnknownDatabaseSchemaError(SchemaIdentityError):
    """Raised before writes when a nonempty database is not the exact release schema."""


class BaselineApplyError(SchemaIdentityError):
    """Raised when the release baseline cannot be applied atomically."""


class AssetStoreError(StorageError):
    """Base class for content-addressed asset failures."""


class AssetWriteError(AssetStoreError):
    """Raised when an asset cannot be persisted atomically."""


class AssetIntegrityError(AssetStoreError):
    """Raised when an existing asset does not match its digest."""
