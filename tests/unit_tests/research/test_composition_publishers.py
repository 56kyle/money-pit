from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import pytest

from money_pit.composition import ApplicationDependencyError
from money_pit.composition import _research_publisher_definitions  # pyright: ignore[reportPrivateUsage]
from money_pit.composition import build_configured_research_registry
from money_pit.config import ApplicationConfig
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel


if TYPE_CHECKING:
    from money_pit.secrets import ResearchCredentialResolver


def _publisher(source_id: str, *, enabled: bool = True) -> SourceDefinition:
    return SourceDefinition(
        source_id=source_id,
        adapter_name="web",
        enabled=enabled,
        locator=f"https://{source_id}.example/",
        provenance_group=source_id,
        allowed_uses=(AllowedUse.INTERPRETATION, AllowedUse.FACTUAL_VERIFICATION),
        trust_settings=(
            SourceTrustSetting(
                category=TrustCategory.FACTUAL,
                level=TrustLevel.INDEPENDENT_SECONDARY,
            ),
        ),
        tags=("research-publisher", "provider:brave"),
    )


def _config(*definitions: SourceDefinition) -> ApplicationConfig:
    value = SimpleNamespace(sources=SimpleNamespace(sources=definitions))
    return cast("ApplicationConfig", cast("object", value))


def test__research_publisher_definitions_returns_only_enabled_exact_provider_policies() -> None:
    enabled = _publisher("enabled")
    disabled = _publisher("disabled", enabled=False)

    result = _research_publisher_definitions(_config(enabled, disabled), "brave")

    assert result == (enabled,)


def test__research_publisher_definitions_fails_when_no_destination_policy_is_enabled() -> None:
    with pytest.raises(ApplicationDependencyError):
        _ = _research_publisher_definitions(_config(_publisher("disabled", enabled=False)), "brave")


class _RejectCredentials:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"disabled providers must not resolve credential capability {name}")


def test_build_configured_research_registry_does_not_resolve_disabled_provider_credentials() -> None:
    credentials = cast("ResearchCredentialResolver", cast("object", _RejectCredentials()))

    registry = build_configured_research_registry(_config(), credentials=credentials)

    assert registry.names() == ()
