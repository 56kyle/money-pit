"""Shared validation and asset materialization for processor bundles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from money_pit.sources.errors import SourceExtractionError


if TYPE_CHECKING:
    from money_pit.evidence.results import EvidenceProcessingBundle
    from money_pit.schemas.evidence import EvidenceDocument
    from money_pit.schemas.sources import RawArtifact
    from money_pit.storage.assets import AssetStore


def materialize_processing_bundle(
    artifact: RawArtifact,
    bundle: EvidenceProcessingBundle,
    asset_store: AssetStore,
) -> tuple[EvidenceDocument, ...]:
    """Validate every binding and store all immutable bundle assets."""
    documents = (bundle.primary, *(item.document for item in bundle.derived))
    contents = (artifact.content, *(item.content for item in bundle.derived))
    durable: list[EvidenceDocument] = []
    for content, document in zip(contents, documents, strict=True):
        stored = asset_store.put_bytes(content)
        if stored.digest != document.asset.asset_id:
            raise SourceExtractionError("Evidence document content identity changed")
        if document.asset.source_item_id != artifact.source_item.source_item_id:
            raise SourceExtractionError("Evidence document returned the wrong source item")
        if any(fragment.asset_id != stored.digest for fragment in document.fragments):
            raise SourceExtractionError("Evidence fragment returned the wrong asset identity")
        durable.append(
            document.model_copy(
                update={"asset": document.asset.model_copy(update={"local_path": stored.path})},
            ),
        )
    return tuple(durable)
