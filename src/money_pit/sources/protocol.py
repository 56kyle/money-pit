"""Module containing transport-neutral source connector protocols."""

from typing import Protocol
from typing import runtime_checkable

from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceItem


class SourceConnector(Protocol):
    """Discovers, fetches, and extracts one configured source."""

    def discover(
        self,
        cursor: SourceCursor | None,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> DiscoveryBatch:
        """Discover a bounded, idempotent batch of source items."""
        ...

    def fetch(self, item: SourceItem) -> RawArtifact:
        """Fetch one discovered version while enforcing content bounds."""
        ...

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        """Extract traceable evidence without granting execution capabilities."""
        ...


@runtime_checkable
class DirectUrlSourceConnector(Protocol):
    """Connector capability for one caller-selected URL."""

    def source_item_from_url(self, url: str) -> SourceItem:
        """Validate a URL and return its stable configured-source identity."""
        ...

    @property
    def maximum_artifact_bytes(self) -> int:
        """Return the byte bound that cached raw content must satisfy."""
        ...
