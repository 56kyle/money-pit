"""Module containing versioned source registry loading and adapter lookup."""

from collections.abc import Callable
from pathlib import Path

import tomllib
from pydantic import ValidationError

from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.sources.errors import AdapterAlreadyRegisteredError
from money_pit.sources.errors import DuplicateSourceIdError
from money_pit.sources.errors import SourceRegistryReadError
from money_pit.sources.errors import UnknownAdapterError
from money_pit.sources.errors import UnsupportedSourceRegistryVersionError
from money_pit.sources.protocol import SourceConnector


CURRENT_SOURCE_REGISTRY_VERSION = "0.0.2"
ConnectorFactory = Callable[[SourceDefinition], SourceConnector]


class AdapterRegistry:
    """Explicit connector factory registry with duplicate protection."""

    def __init__(self) -> None:
        """Create an empty explicit adapter registry."""
        self._factories: dict[str, ConnectorFactory] = {}

    def register(self, adapter_name: str, factory: ConnectorFactory) -> None:
        """Register one unique adapter factory."""
        if adapter_name in self._factories:
            raise AdapterAlreadyRegisteredError(f"Adapter already registered: {adapter_name}")
        self._factories[adapter_name] = factory

    def create(self, definition: SourceDefinition) -> SourceConnector:
        """Construct the connector named by a validated definition."""
        try:
            factory: ConnectorFactory = self._factories[definition.adapter_name]
        except KeyError as error:
            raise UnknownAdapterError(
                f"Unknown adapter {definition.adapter_name!r} for source {definition.source_id!r}",
            ) from error
        return factory(definition)

    def contains(self, adapter_name: str) -> bool:
        """Return whether an adapter name is registered."""
        return adapter_name in self._factories


def load_source_registry(
    path: Path,
    adapters: AdapterRegistry,
) -> SourceRegistryDocument:
    """Read and strictly validate a versioned sources.toml document."""
    try:
        raw_bytes: bytes = path.read_bytes()
        raw: object = tomllib.loads(raw_bytes.decode("utf-8"))
        document: SourceRegistryDocument = SourceRegistryDocument.model_validate(raw)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise SourceRegistryReadError(f"Cannot load source registry {path}") from error
    if document.version != CURRENT_SOURCE_REGISTRY_VERSION:
        raise UnsupportedSourceRegistryVersionError(
            f"Unsupported source registry version: {document.version}",
        )
    source_ids: tuple[str, ...] = tuple(source.source_id for source in document.sources)
    if len(source_ids) != len(set(source_ids)):
        raise DuplicateSourceIdError("Source registry contains duplicate source_id values")
    for source in document.sources:
        if not adapters.contains(source.adapter_name):
            raise UnknownAdapterError(
                f"Unknown adapter {source.adapter_name!r} for source {source.source_id!r}",
            )
    return document
