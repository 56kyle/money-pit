"""Module containing transport-neutral research provider contracts."""

from typing import ClassVar
from typing import Protocol

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.research import ResearchDiscoveryBatch
from money_pit.schemas.research import ResearchDiscoveryResult
from money_pit.schemas.research import ResearchQuery
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceDefinition


class ResearchProviderCapabilities(BaseModel):
    """Auditable guarantees a provider makes about historical retrieval."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    point_in_time_certified: bool = False
    availability_is_verifiable: bool = False


class ResearchProvider(Protocol):
    """Discovers and fetches read-only research material."""

    @property
    def name(self) -> str:
        """Return the stable provider name agents may request."""
        ...

    def source_definition_for(self, result: ResearchDiscoveryResult) -> SourceDefinition:
        """Resolve the immutable publisher policy for one fetched result."""
        ...

    @property
    def capabilities(self) -> ResearchProviderCapabilities:
        """Return explicit point-in-time retrieval guarantees."""
        ...

    def search(self, query: ResearchQuery) -> ResearchDiscoveryBatch:
        """Discover bounded metadata that is not itself verification evidence."""
        ...

    def fetch(self, result: ResearchDiscoveryResult) -> RawArtifact:
        """Fetch one result into a complete bounded acquisition."""
        ...

    def search_as_of(
        self,
        query: ResearchQuery,
        *,
        requested_as_of: AwareDatetime,
    ) -> ResearchDiscoveryBatch:
        """Search a certified immutable point-in-time index."""
        ...

    def fetch_as_of(
        self,
        result: ResearchDiscoveryResult,
        *,
        requested_as_of: AwareDatetime,
    ) -> RawArtifact:
        """Fetch the exact certified version available at the cutoff."""
        ...
