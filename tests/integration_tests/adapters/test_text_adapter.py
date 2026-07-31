"""Integration tests for TextAdapter: TextPayload → persist → stub agent → SignalSet."""

from collections.abc import Callable
from pathlib import Path

import pytest

from money_pit.adapters.text import TextAdapter
from money_pit.adapters.text_llm import TextPayload
from money_pit.constants import source_id_to_dirname
from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signal_draft import ClaimDraft
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.schemas.signals import SignalSet


@pytest.fixture
def source_ref() -> SourceRef:
    return SourceRef(
        source_id="note:ab12cd34",
        source_type=SourceType.MANUAL_NOTE,
        title="NVDA Thesis Note",
        url=None,
        published_at=None,
        retrieved_at="2026-06-18T14:30:00Z",
        locator=None,
    )


@pytest.fixture
def text_payload(source_ref: SourceRef) -> TextPayload:
    return TextPayload(
        slug="2026-06-18_14-30-00",
        source_ref=source_ref,
        body=(
            "NVDA is well-positioned for AI infrastructure buildout. "
            "The data center segment continues to show 200%+ growth. "
            "Risk: AMD competition increasing."
        ),
    )


def _high_claim() -> ClaimDraft:
    return ClaimDraft(
        claim_id="claim_001",
        claim="NVDA data center segment shows 200%+ YoY growth driven by AI infrastructure buildout.",
        tier=SignalTier.HIGH.value,
        category=ClaimCategory.FUNDAMENTAL.value,
        tickers_affected=["NVDA"],
        cited_sources=[],
    )


def _low_claim() -> ClaimDraft:
    return ClaimDraft(
        claim_id="claim_002",
        claim="AMD competition is increasing in the GPU space.",
        tier=SignalTier.LOW.value,
        category=ClaimCategory.SENTIMENT.value,
        tickers_affected=["AMD"],
        cited_sources=[],
    )


def _make_draft(source_ref: SourceRef, claims: list[ClaimDraft]) -> SignalSetDraft:
    return SignalSetDraft(
        source_id=source_ref.source_id,
        source_type=source_ref.source_type.value,
        title=source_ref.title,
        url=source_ref.url,
        published_at=source_ref.published_at,
        retrieved_at=source_ref.retrieved_at,
        summary="NVDA is well-positioned for AI infrastructure. Risk is AMD competition.",
        claims=claims,
        tickers_mentioned=["NVDA", "AMD"],
        sectors_mentioned=["Technology", "Semiconductors"],
        macro_themes=["AI infrastructure buildout"],
    )


@pytest.fixture
def stub_agent(source_ref: SourceRef) -> Callable[[TextPayload], SignalSetDraft]:
    def _agent(_payload: TextPayload) -> SignalSetDraft:
        return _make_draft(source_ref, [_high_claim(), _low_claim()])

    return _agent


@pytest.fixture
def processed_signal_set(
    stub_agent: Callable[[TextPayload], SignalSetDraft],
    text_payload: TextPayload,
    tmp_path: Path,
) -> tuple[SignalSet, Path]:
    result = TextAdapter(agent=stub_agent, cache_dir=tmp_path).process(text_payload)
    return result, tmp_path


def test_text_adapter_process_returns_signal_set(processed_signal_set: tuple[SignalSet, Path]) -> None:
    result, _ = processed_signal_set
    assert isinstance(result, SignalSet)


def test_text_adapter_process_preserves_source_id(processed_signal_set: tuple[SignalSet, Path]) -> None:
    result, _ = processed_signal_set
    assert result.source_ref.source_id == "note:ab12cd34"


def test_text_adapter_process_preserves_slug(processed_signal_set: tuple[SignalSet, Path]) -> None:
    result, _ = processed_signal_set
    assert result.slug == "2026-06-18_14-30-00"


def test_text_adapter_process_preserves_summary(processed_signal_set: tuple[SignalSet, Path]) -> None:
    result, _ = processed_signal_set
    assert result.summary == "NVDA is well-positioned for AI infrastructure. Risk is AMD competition."


def test_text_adapter_process_carries_draft_metadata(processed_signal_set: tuple[SignalSet, Path]) -> None:
    result, _ = processed_signal_set
    assert result.tickers_mentioned == ["NVDA", "AMD"]
    assert result.sectors_mentioned == ["Technology", "Semiconductors"]
    assert result.macro_themes == ["AI infrastructure buildout"]


def test_text_adapter_process_converts_claim_ids(processed_signal_set: tuple[SignalSet, Path]) -> None:
    result, _ = processed_signal_set
    assert [claim.claim_id for claim in result.claims] == ["claim_001", "claim_002"]


def test_text_adapter_process_computes_requires_validation_for_high_claim(
    processed_signal_set: tuple[SignalSet, Path],
) -> None:
    result, _ = processed_signal_set
    assert result.claims[0].requires_validation is True


def test_text_adapter_process_computes_requires_validation_for_low_claim(
    processed_signal_set: tuple[SignalSet, Path],
) -> None:
    result, _ = processed_signal_set
    assert result.claims[1].requires_validation is False


def test_text_adapter_process_computes_has_actionable_content(
    processed_signal_set: tuple[SignalSet, Path],
) -> None:
    result, _ = processed_signal_set
    assert result.has_actionable_content is True


@pytest.fixture
def persisted_payload_path(processed_signal_set: tuple[SignalSet, Path], source_ref: SourceRef) -> Path:
    _, cache_dir = processed_signal_set
    return cache_dir / source_id_to_dirname(source_ref.source_id) / "text_payload.json"


def test_text_adapter_persists_payload_before_llm(persisted_payload_path: Path) -> None:
    assert persisted_payload_path.exists()


def test_text_adapter_persisted_payload_round_trips(persisted_payload_path: Path, text_payload: TextPayload) -> None:
    round_tripped = TextPayload.model_validate_json(persisted_payload_path.read_text(encoding="utf-8"))
    assert round_tripped == text_payload
