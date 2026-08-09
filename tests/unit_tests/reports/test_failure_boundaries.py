"""Failure contracts for static portfolio-report persistence."""

from pathlib import Path

import pytest

from money_pit.reports.portfolio import PortfolioReview
from money_pit.reports.portfolio import ReportReadError
from money_pit.reports.portfolio import ReportRenderError
from money_pit.reports.portfolio import load_portfolio_review
from money_pit.reports.portfolio import write_report_bundle
from money_pit.schemas.portfolio_plan import PortfolioPlan


def _review(plan: PortfolioPlan) -> PortfolioReview:
    return PortfolioReview(
        title="Portfolio review",
        plan=plan,
        current_weights={},
        risk_contributions={},
        sector_exposures={},
        factor_exposures={},
        thesis_statuses={},
        conflicting_claims=(),
        source_authority_ratio_by_claim_category={},
        evidence=(),
        scenarios=(),
        decision_diagnostics=(),
        approval_state="approval_required",
        execution_state="not_executed",
    )


def test_load_portfolio_review_missing_path_raises_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ReportReadError):
        _ = load_portfolio_review(tmp_path / "missing.json")


def test_write_report_bundle_rejects_existing_file(
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report"
    _ = report_path.write_text("occupied", encoding="utf-8")

    with pytest.raises(ReportRenderError):
        _ = write_report_bundle(_review(portfolio_plan), report_path)


def test_write_report_bundle_with_failed_install_leaves_no_partial_bundle(
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "report"

    def fail_install(_source: Path, _target: Path) -> None:
        raise PermissionError("install failed")

    monkeypatch.setattr(Path, "replace", fail_install)

    with pytest.raises(PermissionError):
        _ = write_report_bundle(_review(portfolio_plan), report_path)

    assert not report_path.exists()


def test_write_report_bundle_with_exact_existing_bundle_is_idempotent(
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report"
    review = _review(portfolio_plan)
    first = write_report_bundle(review, report_path)

    second = write_report_bundle(review, report_path)

    assert second == first


def test_write_report_bundle_with_different_existing_bundle_fails_closed(
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report"
    review = _review(portfolio_plan)
    _ = write_report_bundle(review, report_path)
    changed = review.model_copy(update={"title": "Different review"})

    with pytest.raises(ReportRenderError):
        _ = write_report_bundle(changed, report_path)
