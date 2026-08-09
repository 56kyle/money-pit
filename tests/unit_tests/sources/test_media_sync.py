import hashlib
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest


if TYPE_CHECKING:
    import sqlite3

from money_pit.evidence.media import FrameReading
from money_pit.evidence.media import MediaAnalysis
from money_pit.evidence.media import MediaEvidenceProcessor
from money_pit.evidence.media import TimedTranscriptSegment
from money_pit.evidence.processors import EvidenceProcessorRegistry
from money_pit.evidence.repository import EvidenceProcessingAttemptRepository
from money_pit.evidence.results import DerivedEvidenceDocument
from money_pit.evidence.results import EvidenceProcessingBundle
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TimestampLocator
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.schemas.sources import SourceRegistryDocument
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.sources._shared import evidence_asset
from money_pit.sources._shared import source_definition_hash
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.registry import AdapterRegistry
from money_pit.sources.service import EvidenceRepository
from money_pit.sources.service import SourceSyncService
from money_pit.storage.assets import AssetStore
from money_pit.storage.database import Database
from money_pit.storage.sources import SourceRepository


_NOW = datetime(2026, 8, 1, tzinfo=UTC)
_VIDEO = b"bounded-video-content"
_FRAME = b"\x89PNG\r\n\x1a\nframe-content"


class _MediaAnalyzer:
    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        assert path.read_bytes() == _VIDEO
        assert media_type == "video/mp4"
        return MediaAnalysis(
            transcript=(TimedTranscriptSegment(start_seconds=0, end_seconds=1, text="Revenue rose."),),
            frames=(
                FrameReading(
                    timestamp_seconds=0.5,
                    on_screen_text=("Revenue +20%",),
                    cited_sources=("SEC filing",),
                    bounding_box=(0.1, 0.2, 0.8, 0.9),
                    confidence=0.95,
                    extraction_model="vision-test",
                    image_png=_FRAME,
                ),
            ),
        )


class _VideoConnector:
    def __init__(self, definition: SourceDefinition) -> None:
        self.definition: SourceDefinition = definition
        digest = hashlib.sha256(_VIDEO).hexdigest()
        self.item: SourceItem = SourceItem(
            source_item_id="video:item",
            source_id=definition.source_id,
            source_definition_hash=source_definition_hash(definition),
            canonical_uri="https://example.com/video",
            discovered_at=_NOW,
            content_version=digest,
        )

    def discover(
        self,
        cursor: SourceCursor | None,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> DiscoveryBatch:
        del purpose
        return DiscoveryBatch(
            items=() if cursor is not None else (self.item,),
            next_cursor=SourceCursor(value=self.item.content_version),
            discovered_at=_NOW,
        )

    def fetch(self, item: SourceItem) -> RawArtifact:
        return RawArtifact(
            source_item=item,
            content=_VIDEO,
            media_type="video/mp4",
            retrieved_at=_NOW,
            canonical_uri=item.canonical_uri,
            filename=Path("video.mp4"),
            content_hash=hashlib.sha256(_VIDEO).hexdigest(),
        )

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        raise AssertionError(artifact)


class _WrongFragmentProcessor:
    name: str = "wrong-fragment"
    version: str = "1"

    def __init__(self, location: str) -> None:
        self._location: str = location

    def supports(self, media_type: str) -> bool:
        return media_type == "video/mp4"

    def process_bundle(self, acquisition: RawArtifact) -> EvidenceProcessingBundle:
        artifact = acquisition
        primary_fragment = EvidenceFragment(
            fragment_id="primary-fragment",
            asset_id="f" * 64 if self._location == "primary" else artifact.content_hash,
            kind="transcript",
            locator=TimestampLocator(start_seconds=0),
            extracted_text="text",
            extraction_method=self.name,
        )
        derived_content = b"derived-frame"
        derived_digest = hashlib.sha256(derived_content).hexdigest()
        derived_fragment = EvidenceFragment(
            fragment_id="derived-fragment",
            asset_id="f" * 64 if self._location == "derived" else derived_digest,
            kind="frame",
            locator=TimestampLocator(start_seconds=1),
            extracted_text="frame",
            extraction_method=self.name,
        )
        return EvidenceProcessingBundle(
            primary=EvidenceDocument(
                asset=evidence_asset(artifact),
                fragments=(primary_fragment,),
            ),
            derived=(
                DerivedEvidenceDocument(
                    content=derived_content,
                    document=EvidenceDocument(
                        asset=evidence_asset(artifact).model_copy(
                            update={
                                "asset_id": derived_digest,
                                "content_hash": derived_digest,
                                "media_type": "image/png",
                            },
                        ),
                        fragments=(derived_fragment,),
                    ),
                ),
            ),
        )


def test_source_sync_persists_content_addressed_frame_assets_from_the_generic_bundle(tmp_path: Path) -> None:
    definition = SourceDefinition(
        source_id="video",
        adapter_name="test-video",
        locator="https://example.com/video",
        provenance_group="publisher",
        allowed_uses=(AllowedUse.INTERPRETATION, AllowedUse.THESIS_GENERATION),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.COMMENTARY),),
    )
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    adapters = AdapterRegistry()
    adapters.register("test-video", _VideoConnector)
    processors = EvidenceProcessorRegistry()
    processors.register(MediaEvidenceProcessor(_MediaAnalyzer()))
    service = SourceSyncService(
        SourceRegistryDocument(version="0.0.2", sources=(definition,)),
        adapters,
        SourceRepository(database),
        EvidenceRepository(database),
        AssetStore(tmp_path / "assets"),
        attempt_repository=EvidenceProcessingAttemptRepository(database),
        processor_registry=processors,
    )
    _ = service.register_definitions()

    _ = service.sync(definition.source_id)

    frame_digest = hashlib.sha256(_FRAME).hexdigest()
    with database.transaction() as connection:
        frame_asset = cast(
            "sqlite3.Row | None",
            connection.execute(
                "SELECT content_hash, local_path FROM evidence_assets WHERE asset_id = ?",
                (frame_digest,),
            ).fetchone(),
        )
        frame_fragments = cast(
            "list[sqlite3.Row]",
            connection.execute(
                "SELECT extracted_text, cited_source_text FROM evidence_fragments WHERE asset_id = ? ORDER BY fragment_id",
                (frame_digest,),
            ).fetchall(),
        )
        attempts = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """SELECT asset_id, status, processor_name, fragment_ids_json
                FROM evidence_processing_attempts ORDER BY asset_id""",
            ).fetchall(),
        )
    assert frame_asset is not None
    assert Path(str(cast("object", frame_asset["local_path"]))).read_bytes() == _FRAME
    assert {
        (
            cast("object", row["extracted_text"]),
            cast("object", row["cited_source_text"]),
        )
        for row in frame_fragments
    } == {
        ("Revenue +20%", None),
        (None, "SEC filing"),
    }
    assert len(attempts) == 2
    assert {
        (
            cast("object", attempt["asset_id"]),
            cast("object", attempt["status"]),
            cast("object", attempt["processor_name"]),
        )
        for attempt in attempts
    } == {
        (hashlib.sha256(_VIDEO).hexdigest(), "succeeded", "timestamped-media"),
        (frame_digest, "succeeded", "timestamped-media"),
    }
    assert any(frame_digest in str(cast("object", attempt["fragment_ids_json"])) for attempt in attempts)


@pytest.mark.parametrize("location", ["primary", "derived"])
def test_source_sync_rejects_fragments_bound_to_the_wrong_asset(
    tmp_path: Path,
    location: str,
) -> None:
    definition = SourceDefinition(
        source_id="video",
        adapter_name="test-video",
        locator="https://example.com/video",
        provenance_group="publisher",
        allowed_uses=(AllowedUse.INTERPRETATION,),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.COMMENTARY),),
    )
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    adapters = AdapterRegistry()
    adapters.register("test-video", _VideoConnector)
    processors = EvidenceProcessorRegistry()
    processors.register(_WrongFragmentProcessor(location))
    service = SourceSyncService(
        SourceRegistryDocument(version="0.0.2", sources=(definition,)),
        adapters,
        SourceRepository(database),
        EvidenceRepository(database),
        AssetStore(tmp_path / "assets"),
        attempt_repository=EvidenceProcessingAttemptRepository(database),
        processor_registry=processors,
    )
    _ = service.register_definitions()

    with pytest.raises(SourceDiscoveryError):
        _ = service.sync(definition.source_id)
