"""Tests for the last reachable branch edges in deterministic legacy helpers."""

from pathlib import Path

from money_pit.compute.aggregation import tier_max
from money_pit.ingestion.fetch import _first_match
from money_pit.pipeline.notification import _build_body
from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import ClaimRelationType
from money_pit.schemas.enums import OverallValidationStatus
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signals import Claim
from money_pit.schemas.signals import CorroborationEntry
from money_pit.schemas.validation_results import ActionStepsValidation


def _claim() -> Claim:
    source = SourceRef(
        source_id="source-1",
        source_type=SourceType.MANUAL_NOTE,
        title="Source",
        url=None,
        published_at=None,
        retrieved_at="2026-07-29T14:00:00Z",
        locator=None,
    )
    return Claim(
        claim_id="claim-1",
        tier=SignalTier.LOW,
        claim="Claim",
        category=ClaimCategory.FUNDAMENTAL,
        tickers_affected=[],
        requires_validation=False,
        source_ref=source,
        cited_sources=[],
    )


def test_tier_max_with_known_and_unknown_ids_skips_unknown_member() -> None:
    claim = _claim()
    relation = CorroborationEntry(
        relation=ClaimRelationType.AGREE,
        claim_ids=["unknown", claim.claim_id],
    )

    assert tier_max([claim], [relation]) == [claim]


def test__first_match_with_no_match_returns_none(tmp_path: Path) -> None:
    assert _first_match(tmp_path, "*.json") is None


def test__first_match_returns_sorted_first_match(tmp_path: Path) -> None:
    second = tmp_path / "b.json"
    first = tmp_path / "a.json"
    second.touch()
    first.touch()

    assert _first_match(tmp_path, "*.json") == first


def test__build_body_with_validation_but_no_unmatched_steps_adds_no_gap_section() -> None:
    validation = ActionStepsValidation(
        slug="run-1",
        overall_status=OverallValidationStatus.VALIDATED,
        steps=[],
    )

    body = _build_body("run-1", None, None, [], validation, None)

    assert "Unmatched validation steps" not in body
