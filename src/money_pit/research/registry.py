"""Module containing an explicit read-only research provider registry."""

from money_pit.research.errors import ResearchProviderAlreadyRegisteredError
from money_pit.research.errors import ResearchProviderNotFoundError
from money_pit.research.protocol import ResearchProvider


class ResearchProviderRegistry:
    """Registry that exposes only explicitly configured research providers."""

    def __init__(self) -> None:
        """Create an empty provider registry."""
        self._providers: dict[str, ResearchProvider] = {}

    def register(self, provider: ResearchProvider) -> None:
        """Register a uniquely named provider."""
        if provider.name in self._providers:
            raise ResearchProviderAlreadyRegisteredError(
                f"Research provider already registered: {provider.name}",
            )
        self._providers[provider.name] = provider

    def get(self, provider_name: str) -> ResearchProvider:
        """Return an allowed provider or fail before external I/O."""
        try:
            return self._providers[provider_name]
        except KeyError as error:
            raise ResearchProviderNotFoundError(
                f"Research provider is not registered: {provider_name}",
            ) from error

    def names(self) -> tuple[str, ...]:
        """Return configured provider names in deterministic order."""
        return tuple(sorted(self._providers))
