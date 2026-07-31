import pytest

from money_pit.schemas.sources import SourceDefinition
from money_pit.sources.errors import AdapterAlreadyRegisteredError
from money_pit.sources.errors import DuplicateSourceIdError
from money_pit.sources.errors import SourceRegistryReadError
from money_pit.sources.errors import UnknownAdapterError
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.registry import load_source_registry


def _factory(definition):
    return definition


def test_load_source_registry_with_valid_document(tmp_path):
    registry_path = tmp_path / "sources.toml"
    registry_path.write_text(
        """
version = 1
[[sources]]
source_id = "market-news"
adapter_name = "test"
locator = "https://example.com/feed"
tags = ["macro"]
""".strip(),
        encoding="utf-8",
    )
    adapters = AdapterRegistry()
    adapters.register("test", _factory)

    document = load_source_registry(registry_path, adapters)

    assert document.sources == (
        SourceDefinition(
            source_id="market-news",
            adapter_name="test",
            locator="https://example.com/feed",
            tags=("macro",),
        ),
    )


def test_load_source_registry_rejects_duplicate_source_id(tmp_path):
    registry_path = tmp_path / "sources.toml"
    registry_path.write_text(
        """
version = 1
[[sources]]
source_id = "same"
adapter_name = "test"
locator = "https://example.com/one"
[[sources]]
source_id = "same"
adapter_name = "test"
locator = "https://example.com/two"
""".strip(),
        encoding="utf-8",
    )
    adapters = AdapterRegistry()
    adapters.register("test", _factory)

    with pytest.raises(DuplicateSourceIdError):
        load_source_registry(registry_path, adapters)


def test_load_source_registry_rejects_unknown_adapter(tmp_path):
    registry_path = tmp_path / "sources.toml"
    registry_path.write_text(
        """
version = 1
[[sources]]
source_id = "source"
adapter_name = "missing"
locator = "https://example.com"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(UnknownAdapterError):
        load_source_registry(registry_path, AdapterRegistry())


def test_load_source_registry_rejects_malformed_toml(tmp_path):
    registry_path = tmp_path / "sources.toml"
    registry_path.write_text("version = [", encoding="utf-8")

    with pytest.raises(SourceRegistryReadError):
        load_source_registry(registry_path, AdapterRegistry())


def test_register_rejects_duplicate_adapter():
    adapters = AdapterRegistry()
    adapters.register("test", _factory)

    with pytest.raises(AdapterAlreadyRegisteredError):
        adapters.register("test", _factory)
