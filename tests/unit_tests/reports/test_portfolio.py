"""Tests for static portfolio-review reports."""

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import HttpUrl
from pydantic import ValidationError

from money_pit.reports.portfolio import PortfolioReview
from money_pit.reports.portfolio import ReportEvidence
from money_pit.reports.portfolio import ReportRenderError
from money_pit.reports.portfolio import load_portfolio_review
from money_pit.reports.portfolio import render_html
from money_pit.reports.portfolio import render_json
from money_pit.reports.portfolio import render_markdown
from money_pit.reports.portfolio import write_report_bundle
from money_pit.schemas.portfolio_plan import PortfolioPlan


def _review(plan: PortfolioPlan) -> PortfolioReview:
    return PortfolioReview(
        title="Portfolio <Review>",
        plan=plan,
        current_weights={"AAPL": 0.1, "SPY": 0.6},
        risk_contributions={"AAPL": 0.04, "SPY": 0.1},
        sector_exposures={"technology": 0.1},
        factor_exposures={"market": 0.7},
        thesis_statuses={"AAPL": "active"},
        conflicting_claims=("Demand is high | supply is constrained",),
        source_authority_ratio_by_claim_category={"filing": 0.9},
        evidence=(
            ReportEvidence(
                label="Frame <script>alert(1)</script>",
                web_url=HttpUrl("https://youtu.be/example?t=30"),
                source_item_id="video-1",
                status="supported",
            ),
        ),
        scenarios=({"name": "base", "probability": 0.5},),
        decision_diagnostics=({"turnover": 0.1},),
        approval_state="approval_required",
        execution_state="not_executed",
    )


def test_render_json_round_trips_typed_review(portfolio_plan: PortfolioPlan) -> None:
    review: PortfolioReview = _review(portfolio_plan)

    assert PortfolioReview.model_validate_json(render_json(review)) == review


def test_render_markdown_escapes_table_structure(portfolio_plan: PortfolioPlan) -> None:
    rendered: str = render_markdown(_review(portfolio_plan))

    assert r"Demand is high \| supply is constrained" in rendered


def test_render_markdown_labels_source_authority_ratio_honestly(portfolio_plan: PortfolioPlan) -> None:
    rendered = render_markdown(_review(portfolio_plan))

    assert "Source authority ratio by claim category" in rendered
    assert "0.9" in rendered


def test_render_markdown_includes_deterministic_decision_diagnostics(
    portfolio_plan: PortfolioPlan,
) -> None:
    rendered = render_markdown(_review(portfolio_plan))

    assert "Decision diagnostics" in rendered
    assert '"turnover": 0.1' in rendered


def test_render_html_escapes_untrusted_evidence(portfolio_plan: PortfolioPlan) -> None:
    rendered: str = render_html(_review(portfolio_plan))

    assert "<script>alert(1)</script>" not in rendered


@pytest.mark.parametrize(
    "renderer",
    [pytest.param(render_markdown, id="markdown"), pytest.param(render_html, id="html")],
)
def test_render_report_preserves_timestamp_url_and_local_thumbnail(
    portfolio_plan: PortfolioPlan,
    renderer: Callable[[PortfolioReview], str],
) -> None:
    evidence = ReportEvidence(
        label="Video frame at 00:30",
        web_url=HttpUrl("https://youtu.be/example?t=30"),
        local_thumbnail=Path("thumbnails") / f"{'a' * 64}.png",
        source_item_id="video-1",
        status="supported",
    )
    review = _review(portfolio_plan).model_copy(update={"evidence": (evidence,)})

    rendered = renderer(review)

    assert "https://youtu.be/example?t=30" in rendered
    assert f"thumbnails/{'a' * 64}.png" in rendered


def test_report_evidence_rejects_unavailable_reason_with_a_locator() -> None:
    with pytest.raises(ValidationError):
        _ = ReportEvidence(
            label="Video frame at 00:30",
            web_url=HttpUrl("https://youtu.be/example?t=30"),
            unavailable_reason="not available",
            source_item_id="video-1",
            status="supported",
        )


def test_render_markdown_neutralizes_untrusted_remote_image(portfolio_plan: PortfolioPlan) -> None:
    unsafe_title: str = "![remote](https://example.invalid/tracker.png)"
    review: PortfolioReview = _review(portfolio_plan).model_copy(update={"title": unsafe_title})

    rendered: str = render_markdown(review)
    assert unsafe_title not in rendered


@pytest.mark.parametrize(
    "renderer",
    [pytest.param(render_markdown, id="markdown"), pytest.param(render_html, id="html")],
)
def test_render_report_includes_rejected_alternatives(
    portfolio_plan: PortfolioPlan,
    renderer: Callable[[PortfolioReview], str],
) -> None:
    assert "XYZ" in renderer(_review(portfolio_plan))


@pytest.mark.parametrize(
    "renderer",
    [pytest.param(render_markdown, id="markdown"), pytest.param(render_html, id="html")],
)
def test_render_report_includes_scenarios(
    portfolio_plan: PortfolioPlan,
    renderer: Callable[[PortfolioReview], str],
) -> None:
    assert "base" in renderer(_review(portfolio_plan))


@pytest.mark.parametrize(
    "renderer",
    [pytest.param(render_markdown, id="markdown"), pytest.param(render_html, id="html")],
)
def test_render_report_includes_turnover(
    portfolio_plan: PortfolioPlan,
    renderer: Callable[[PortfolioReview], str],
) -> None:
    assert "0.1" in renderer(_review(portfolio_plan))


def test_write_report_bundle_writes_all_formats(
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    paths: tuple[Path, Path, Path] = write_report_bundle(_review(portfolio_plan), tmp_path / "report")

    assert tuple(path.suffix for path in paths) == (".json", ".md", ".html")


def test_load_portfolio_review_reads_written_json(
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    review: PortfolioReview = _review(portfolio_plan)
    path: Path = tmp_path / "review.json"
    _ = path.write_text(render_json(review), encoding="utf-8")

    assert load_portfolio_review(path) == review


def test_write_report_bundle_copies_thumbnail_bound_to_exact_content_hash(
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    payload = b"bounded thumbnail bytes"
    content_hash = hashlib.sha256(payload).hexdigest()
    relative = Path("thumbnails") / f"{content_hash}.png"
    source = tmp_path / "assets" / content_hash[:2] / content_hash
    source.parent.mkdir(parents=True)
    _ = source.write_bytes(payload)
    evidence = ReportEvidence(
        label="Video frame at 00:30",
        local_thumbnail=relative,
        source_item_id="video-1",
        status="supported",
    )
    review = _review(portfolio_plan).model_copy(update={"evidence": (evidence,)})

    _ = write_report_bundle(
        review,
        tmp_path / "report",
        thumbnail_sources={relative: source},
    )

    assert (tmp_path / "report" / relative).read_bytes() == payload


def test_write_report_bundle_rejects_thumbnail_with_wrong_content_hash(
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    relative = Path("thumbnails") / f"{'a' * 64}.png"
    source = tmp_path / "thumbnail.png"
    _ = source.write_bytes(b"different content")
    evidence = ReportEvidence(
        label="Video frame at 00:30",
        local_thumbnail=relative,
        source_item_id="video-1",
        status="supported",
    )
    review = _review(portfolio_plan).model_copy(update={"evidence": (evidence,)})

    with pytest.raises(ReportRenderError, match="content does not match"):
        _ = write_report_bundle(
            review,
            tmp_path / "report",
            thumbnail_sources={relative: source},
        )
