"""Subpackage containing generic source connectors for the money_pit package."""

from money_pit.sources.builtin import builtin_adapter_registry
from money_pit.sources.protocol import DirectUrlSourceConnector
from money_pit.sources.protocol import SourceConnector
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.registry import load_source_registry


__all__ = [
    "AdapterRegistry",
    "DirectUrlSourceConnector",
    "SourceConnector",
    "builtin_adapter_registry",
    "load_source_registry",
]
