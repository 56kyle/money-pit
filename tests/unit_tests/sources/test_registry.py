from pathlib import Path

import pytest

import money_pit.sources.cli as source_cli
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.secrets import SecretSpecResolver
from money_pit.sources.errors import AdapterAlreadyRegisteredError
from money_pit.sources.errors import DuplicateSourceIdError
from money_pit.sources.errors import SourceRegistryReadError
from money_pit.sources.errors import UnknownAdapterError
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.registry import load_source_registry


class _Connector:
    def discover(
        self,
        cursor: SourceCursor | None,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> DiscoveryBatch:
        del cursor, purpose
        raise AssertionError("registry loading must not invoke connectors")

    def fetch(self, item: SourceItem) -> RawArtifact:
        del item
        raise AssertionError("registry loading must not invoke connectors")

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        del artifact
        raise AssertionError("registry loading must not invoke connectors")


def _factory(definition: SourceDefinition) -> _Connector:
    del definition
    return _Connector()


def test_load_source_registry_with_valid_document(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.toml"
    _ = registry_path.write_text(
        """
version = "0.0.2"
[[sources]]
source_id = "market-news"
adapter_name = "test"
locator = "https://example.com/feed"
provenance_group = "market-news"
allowed_uses = ["interpretation"]
trust_settings = [{ category = "factual", level = "commentary" }]
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
            provenance_group="market-news",
            allowed_uses=(AllowedUse.INTERPRETATION,),
            trust_settings=(
                SourceTrustSetting(
                    category=TrustCategory.FACTUAL,
                    level=TrustLevel.COMMENTARY,
                ),
            ),
            tags=("macro",),
        ),
    )


def test_load_source_registry_rejects_duplicate_source_id(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.toml"
    _ = registry_path.write_text(
        """
version = "0.0.2"
[[sources]]
source_id = "same"
adapter_name = "test"
locator = "https://example.com/one"
provenance_group = "same-one"
allowed_uses = ["interpretation"]
trust_settings = [{ category = "factual", level = "commentary" }]
[[sources]]
source_id = "same"
adapter_name = "test"
locator = "https://example.com/two"
provenance_group = "same-two"
allowed_uses = ["interpretation"]
trust_settings = [{ category = "factual", level = "commentary" }]
""".strip(),
        encoding="utf-8",
    )
    adapters = AdapterRegistry()
    adapters.register("test", _factory)

    with pytest.raises(DuplicateSourceIdError):
        _ = load_source_registry(registry_path, adapters)


def test_load_source_registry_rejects_unknown_adapter(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.toml"
    _ = registry_path.write_text(
        """
version = "0.0.2"
[[sources]]
source_id = "source"
adapter_name = "missing"
locator = "https://example.com"
provenance_group = "source"
allowed_uses = ["interpretation"]
trust_settings = [{ category = "factual", level = "commentary" }]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(UnknownAdapterError):
        _ = load_source_registry(registry_path, AdapterRegistry())


def test_load_source_registry_rejects_malformed_toml(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.toml"
    _ = registry_path.write_text("version = [", encoding="utf-8")

    with pytest.raises(SourceRegistryReadError):
        _ = load_source_registry(registry_path, AdapterRegistry())


def test_register_rejects_duplicate_adapter() -> None:
    adapters = AdapterRegistry()
    adapters.register("test", _factory)

    with pytest.raises(AdapterAlreadyRegisteredError):
        adapters.register("test", _factory)


def test__source_repository_for_listing_does_not_construct_secret_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "sources.toml"
    _ = registry_path.write_text(
        """
version = "0.0.2"
[[sources]]
source_id = "local"
adapter_name = "local_text"
locator = "local.txt"
provenance_group = "local"
allowed_uses = ["interpretation"]
trust_settings = [{ category = "factual", level = "commentary" }]
""".strip(),
        encoding="utf-8",
    )

    def reject_resolution() -> None:
        raise AssertionError("source list must not construct a credential resolver")

    monkeypatch.setattr(source_cli, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(SecretSpecResolver, "from_environment", reject_resolution)

    repository = source_cli._source_repository_for_listing(registry_path)  # pyright: ignore[reportPrivateUsage]

    assert tuple(definition.source_id for definition in repository.list_definitions()) == ("local",)
