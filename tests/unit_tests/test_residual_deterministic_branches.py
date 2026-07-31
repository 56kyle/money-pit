"""Tests for residual deterministic branches exposed by execution-mode rerouting."""

import pytest
from pytest import MonkeyPatch

from money_pit.compute.aggregation import tier_max
from money_pit.compute.factor_profile import aggregate_factor_profile
from money_pit.compute.sizing import solve_kelly
from money_pit.config import Config
from money_pit.ingestion.fetch import make_ytdlp_downloader
from money_pit.ingestion.keyframes import make_opencv_extractor
from money_pit.ingestion.keyframes import make_scenedetect_detector
from money_pit.pipeline.aggregator import _render_aggregated_md
from money_pit.pipeline.notification import _build_body
from money_pit.pipeline.notification import _build_execution_incomplete_body
from money_pit.scheduler import channel
from money_pit.scheduler.channel import make_requests_http_get
from money_pit.schemas.enums import ClaimCategory
from money_pit.schemas.enums import ClaimRelationType
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import SourceType
from money_pit.schemas.portfolio import Position
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.signals import Claim
from money_pit.schemas.signals import CorroborationEntry


def _source_ref() -> SourceRef:
    return SourceRef(
        source_id="source-1",
        source_type=SourceType.MANUAL_NOTE,
        title="Source",
        url=None,
        published_at=None,
        retrieved_at="2026-07-29T14:00:00Z",
        locator=None,
    )


def _claim() -> Claim:
    return Claim(
        claim_id="claim-1",
        tier=SignalTier.LOW,
        claim="Claim",
        category=ClaimCategory.FUNDAMENTAL,
        tickers_affected=[],
        requires_validation=False,
        source_ref=_source_ref(),
        cited_sources=[],
    )


def test_tier_max_with_relation_containing_only_unknown_ids_preserves_claims() -> None:
    claims = [_claim()]
    relation = CorroborationEntry(
        relation=ClaimRelationType.AGREE,
        claim_ids=["unknown"],
    )

    assert tier_max(claims, [relation]) == claims


def test_aggregate_factor_profile_with_untagged_position_contributes_nothing() -> None:
    position = Position(
        ticker="CASH",
        quantity=1.0,
        cost_basis=100.0,
        current_value=100.0,
        unrealized_pl=0.0,
        sector="Cash",
        factor_tags=[],
    )

    result = aggregate_factor_profile([position])

    assert all(value == 0.0 for value in result.values())


def test_solve_kelly_with_only_positive_returns_uses_full_fraction() -> None:
    assert solve_kelly([(1.0, 0.1)]) == pytest.approx(1.0)


def test_solve_kelly_with_zero_iterations_returns_midpoint() -> None:
    assert solve_kelly([(0.7, 0.2), (0.3, -0.1)], max_iter=0) == pytest.approx(0.5)


def test__render_aggregated_md_with_no_claims_renders_empty_marker() -> None:
    aggregated = AggregatedSignals(
        slug="run-1",
        sources=[],
        claims=[],
        corroborations=[],
        conflicts=[],
        has_actionable_content=False,
    )

    assert "- No claims." in _render_aggregated_md(aggregated)


def test__render_aggregated_md_with_relations_renders_each_group() -> None:
    relation = CorroborationEntry(
        relation=ClaimRelationType.AGREE,
        claim_ids=["claim-1", "claim-2"],
    )
    conflict = relation.model_copy(update={"relation": ClaimRelationType.DISAGREE, "claim_ids": ["claim-3"]})
    aggregated = AggregatedSignals(
        slug="run-1",
        sources=[],
        claims=[_claim()],
        corroborations=[relation],
        conflicts=[conflict],
        has_actionable_content=True,
    )

    rendered = _render_aggregated_md(aggregated)

    assert "- claim-1, claim-2" in rendered
    assert "- claim-3" in rendered


def test__build_execution_incomplete_body_with_no_journal_reports_none() -> None:
    body = _build_execution_incomplete_body(
        "run-1",
        ExecutionOutcome.EXECUTED_INCOMPLETE,
        None,
    )

    assert "Execution journal: none" in body


def test__build_body_with_no_action_steps_reports_none() -> None:
    body = _build_body("run-1", None, None, [], None, None)

    assert "Action steps: none" in body


def test_factory_functions_return_callable_without_loading_optional_dependencies() -> None:
    config = Config(
        alpaca_service="alpaca",
        alpaca_username="key",
        alpaca_paper=True,
    )

    assert callable(make_scenedetect_detector(config))
    assert callable(make_opencv_extractor())
    assert callable(make_ytdlp_downloader())
    assert callable(make_requests_http_get())


def test_make_requests_http_get_uses_timeout_and_raises_for_status(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    class Response:
        text = "feed"

        def raise_for_status(self) -> None:
            calls.append(("raised", 0.0))

    def get(url: str, *, timeout: float) -> Response:
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr(channel.requests, "get", get)

    result = make_requests_http_get(timeout=3.0)("https://feed")

    assert result == "feed"
    assert calls == [("https://feed", 3.0), ("raised", 0.0)]
