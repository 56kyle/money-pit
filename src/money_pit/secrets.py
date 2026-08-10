"""Module containing capability-scoped SecretSpec credential resolution."""

# SecretSpec 0.18 provides inline annotations but does not publish a py.typed marker.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Protocol
from typing import Self

from pydantic import SecretStr
from secretspec import MissingRequiredError
from secretspec import SecretSpec
from secretspec import SecretSpecError

from money_pit.config import ConfigurationError
from money_pit.constants import default_config_path
from money_pit.schemas.execution_policy import BrokerEnvironment


if TYPE_CHECKING:
    from pathlib import Path


SECRETSPEC_PROFILE_ENV: str = "SECRETSPEC_PROFILE"
DEFAULT_SECRETSPEC_PROFILE: str = "development"


class CredentialResolutionError(ConfigurationError):
    """Raised when a capability's declared credentials cannot be resolved."""


class SecretSpecConfigurationError(ConfigurationError):
    """Raised when SecretSpec cannot load its manifest, profile, or provider."""


class SecretScope(StrEnum):
    """Named SecretSpec allowlists aligned with runtime capabilities."""

    INFERENCE = "inference"
    YOUTUBE_MEDIA = "youtube_media"
    IMAP = "imap"
    BRAVE = "brave"
    EDGAR = "edgar"
    FRED = "fred"
    PORTFOLIO_PAPER = "portfolio_paper"
    PORTFOLIO_LIVE = "portfolio_live"
    EXECUTION_PAPER = "execution_paper"
    EXECUTION_LIVE = "execution_live"


@dataclass(frozen=True)
class OpenAICredentials:
    """Credential delivered only to OpenAI inference clients."""

    api_key: SecretStr


@dataclass(frozen=True)
class YouTubeMediaCredentials:
    """Credentials required to acquire and interpret YouTube media."""

    youtube_api_key: SecretStr
    openai_api_key: SecretStr


@dataclass(frozen=True)
class ImapCredentials:
    """Credentials delivered only to the bounded IMAP connector."""

    username: SecretStr
    password: SecretStr


@dataclass(frozen=True)
class BraveCredentials:
    """Credential delivered only to Brave search."""

    api_key: SecretStr


@dataclass(frozen=True)
class EdgarCredentials:
    """SEC-compliant identity delivered only to EDGAR clients."""

    user_agent: SecretStr


@dataclass(frozen=True)
class FredCredentials:
    """Credential delivered only to FRED clients."""

    api_key: SecretStr


@dataclass(frozen=True)
class AlpacaCredentials:
    """Resolved Alpaca credentials for one explicit broker environment."""

    api_key: SecretStr
    secret_key: SecretStr
    broker_environment: BrokerEnvironment

    @property
    def paper(self) -> bool:
        """Return the alpaca-py environment flag."""
        return self.broker_environment is BrokerEnvironment.PAPER


class InferenceCredentialResolver(Protocol):
    """Credential authority available to model-backed interpretation."""

    def openai(self, *, reason: str) -> OpenAICredentials:
        """Resolve inference credentials."""
        ...


class SourceCredentialResolver(Protocol):
    """Credential authority available to durable source acquisition."""

    def youtube_media(self, *, reason: str) -> YouTubeMediaCredentials:
        """Resolve YouTube acquisition and frame-inference credentials."""
        ...

    def imap(self, *, reason: str) -> ImapCredentials:
        """Resolve bounded IMAP credentials."""
        ...


class ResearchCredentialResolver(Protocol):
    """Credential authority available to bounded read-only research."""

    def brave(self, *, reason: str) -> BraveCredentials:
        """Resolve Brave research credentials."""
        ...

    def edgar(self, *, reason: str) -> EdgarCredentials:
        """Resolve EDGAR research identity."""
        ...

    def fred(self, *, reason: str) -> FredCredentials:
        """Resolve FRED research credentials."""
        ...


class PortfolioCredentialResolver(Protocol):
    """Credential authority available to read-only portfolio state."""

    def alpaca_portfolio(
        self,
        broker_environment: BrokerEnvironment,
        *,
        reason: str,
    ) -> AlpacaCredentials:
        """Resolve read-only Alpaca credentials for one environment."""
        ...


class ExecutionCredentialResolver(Protocol):
    """Credential authority available only to A6 broker execution."""

    def alpaca_execution(
        self,
        broker_environment: BrokerEnvironment,
        *,
        reason: str,
    ) -> AlpacaCredentials:
        """Resolve write-capable Alpaca credentials for one environment."""
        ...


@dataclass(frozen=True)
class SecretSpecResolver:
    """Resolve one SecretSpec scope at a time without exporting process state."""

    manifest_path: Path
    profile: str = DEFAULT_SECRETSPEC_PROFILE

    @classmethod
    def from_environment(cls, manifest_path: Path | None = None) -> Self:
        """Build a resolver using only the non-secret profile selector."""
        profile: str = os.environ.get(SECRETSPEC_PROFILE_ENV, DEFAULT_SECRETSPEC_PROFILE).strip()
        if not profile:
            raise CredentialResolutionError(f"{SECRETSPEC_PROFILE_ENV} cannot be blank.")
        return cls(manifest_path=manifest_path or default_config_path(), profile=profile)

    def _alpaca(
        self,
        scope: SecretScope,
        broker_environment: BrokerEnvironment,
        *,
        reason: str,
    ) -> AlpacaCredentials:
        fields = self._resolve(scope, reason=reason)
        prefix: str = "ALPACA_PAPER" if broker_environment is BrokerEnvironment.PAPER else "ALPACA_LIVE"
        return AlpacaCredentials(
            api_key=SecretStr(_required(fields, f"{prefix}_API_KEY")),
            secret_key=SecretStr(_required(fields, f"{prefix}_SECRET_KEY")),
            broker_environment=broker_environment,
        )

    def _resolve(self, scope: SecretScope, *, reason: str) -> dict[str, str | None]:
        try:
            resolved = (
                SecretSpec.builder()
                .with_path(str(self.manifest_path))
                .with_profile(self.profile)
                .with_scope(scope.value)
                .with_reason(reason)
                .load()
            )
        except MissingRequiredError as error:
            missing: str = ", ".join(sorted(error.missing))
            raise CredentialResolutionError(
                f"Missing required credentials for SecretSpec scope {scope.value!r}: {missing}."
            ) from error
        except SecretSpecError as error:
            raise SecretSpecConfigurationError(
                f"SecretSpec could not resolve scope {scope.value!r} (kind: {error.kind})."
            ) from error
        try:
            return resolved.fields()
        finally:
            resolved.close()


class SecretSpecInferenceResolver(SecretSpecResolver):
    """SecretSpec resolver exposing only model-inference authority."""

    def openai(self, *, reason: str) -> OpenAICredentials:
        """Resolve inference credentials from the inference scope."""
        fields = self._resolve(SecretScope.INFERENCE, reason=reason)
        return OpenAICredentials(api_key=SecretStr(_required(fields, "OPENAI_API_KEY")))


class SecretSpecSourceResolver(SecretSpecResolver):
    """SecretSpec resolver exposing only source-acquisition authority."""

    def youtube_media(self, *, reason: str) -> YouTubeMediaCredentials:
        """Resolve YouTube and OpenAI credentials from their joint scope."""
        fields = self._resolve(SecretScope.YOUTUBE_MEDIA, reason=reason)
        return YouTubeMediaCredentials(
            youtube_api_key=SecretStr(_required(fields, "YOUTUBE_API_KEY")),
            openai_api_key=SecretStr(_required(fields, "OPENAI_API_KEY")),
        )

    def imap(self, *, reason: str) -> ImapCredentials:
        """Resolve bounded IMAP credentials from the IMAP scope."""
        fields = self._resolve(SecretScope.IMAP, reason=reason)
        return ImapCredentials(
            username=SecretStr(_required(fields, "IMAP_USERNAME")),
            password=SecretStr(_required(fields, "IMAP_PASSWORD")),
        )


class SecretSpecResearchResolver(SecretSpecResolver):
    """SecretSpec resolver exposing only read-only research authority."""

    def brave(self, *, reason: str) -> BraveCredentials:
        """Resolve Brave credentials from the Brave scope."""
        fields = self._resolve(SecretScope.BRAVE, reason=reason)
        return BraveCredentials(api_key=SecretStr(_required(fields, "BRAVE_SEARCH_API_KEY")))

    def edgar(self, *, reason: str) -> EdgarCredentials:
        """Resolve the SEC identity from the EDGAR scope."""
        fields = self._resolve(SecretScope.EDGAR, reason=reason)
        return EdgarCredentials(user_agent=SecretStr(_required(fields, "SEC_USER_AGENT")))

    def fred(self, *, reason: str) -> FredCredentials:
        """Resolve FRED credentials from the FRED scope."""
        fields = self._resolve(SecretScope.FRED, reason=reason)
        return FredCredentials(api_key=SecretStr(_required(fields, "FRED_API_KEY")))


class SecretSpecPortfolioResolver(SecretSpecResolver):
    """SecretSpec resolver exposing only read-only portfolio authority."""

    def alpaca_portfolio(
        self,
        broker_environment: BrokerEnvironment,
        *,
        reason: str,
    ) -> AlpacaCredentials:
        """Resolve only the requested environment's portfolio-read scope."""
        scope = (
            SecretScope.PORTFOLIO_PAPER if broker_environment is BrokerEnvironment.PAPER else SecretScope.PORTFOLIO_LIVE
        )
        return self._alpaca(scope, broker_environment, reason=reason)


class SecretSpecExecutionResolver(SecretSpecResolver):
    """SecretSpec resolver exposing only A6 broker-write authority."""

    def alpaca_execution(
        self,
        broker_environment: BrokerEnvironment,
        *,
        reason: str,
    ) -> AlpacaCredentials:
        """Resolve only the requested environment's execution scope."""
        scope = (
            SecretScope.EXECUTION_PAPER if broker_environment is BrokerEnvironment.PAPER else SecretScope.EXECUTION_LIVE
        )
        return self._alpaca(scope, broker_environment, reason=reason)


def _required(fields: dict[str, str | None], name: str) -> str:
    value: str | None = fields.get(name)
    if value is None or not value.strip():
        raise CredentialResolutionError(f"SecretSpec returned a blank required field: {name}.")
    return value
