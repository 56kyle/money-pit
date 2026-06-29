"""Shared fixtures for compute unit tests."""
from collections.abc import Callable

import pytest

from money_pit.schemas.enums import ClaimCategory, SignalTier, SourceType
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signals import Claim


@pytest.fixture
def source_ref() -> SourceRef:
    return SourceRef(
        source_id="test-src",
        source_type=SourceType.MANUAL_NOTE,
        title="Test Source",
        url=None,
        published_at=None,
        retrieved_at="2026-01-01T00:00:00Z",
        locator=None,
    )


@pytest.fixture
def make_claim(source_ref: SourceRef) -> Callable[[str, SignalTier], Claim]:
    def _make(claim_id: str, tier: SignalTier) -> Claim:
        return Claim(
            claim_id=claim_id,
            tier=tier,
            claim="Test claim.",
            category=ClaimCategory.FUNDAMENTAL,
            tickers_affected=[],
            requires_validation=tier in (SignalTier.HIGH, SignalTier.MEDIUM),
            source_ref=source_ref,
            cited_sources=[],
        )

    return _make
