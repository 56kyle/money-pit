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
        source_reliability={},
        evidence=(),
        scenarios=(),
        sensitivities=(),
        approval_state="approval_required",
        execution_state="not_executed",
    )


def test_load_portfolio_review_missing_path_raises_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ReportReadError):
        load_portfolio_review(tmp_path / "missing.json")


def test_write_report_bundle_rejects_existing_file(
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report"
    _ = report_path.write_text("occupied", encoding="utf-8")

    with pytest.raises(ReportRenderError):
        write_report_bundle(_review(portfolio_plan), report_path)
