"""Module containing transport-neutral source connector protocols."""

from typing import Protocol

from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceItem


class SourceConnector(Protocol):
    """Discovers, fetches, and extracts one configured source."""

    def discover(self, cursor: SourceCursor | None) -> DiscoveryBatch:
        """Discover a bounded, idempotent batch of source items."""
        ...

    def fetch(self, item: SourceItem) -> RawArtifact:
        """Fetch one discovered version while enforcing content bounds."""
        ...

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        """Extract traceable evidence without granting execution capabilities."""
        ...
