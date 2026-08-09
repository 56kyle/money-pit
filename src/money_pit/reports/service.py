"""Module persisting deterministic static portfolio report bundles."""

from pathlib import Path

from money_pit.portfolio.planning import PortfolioPlanningInputs
from money_pit.reports.portfolio import PortfolioReview
from money_pit.reports.portfolio import write_report_bundle
from money_pit.schemas.portfolio_plan import PortfolioPlan


class StaticPortfolioReportWriter:
    """Write one immutable JSON, Markdown, and HTML bundle per plan hash."""

    def __init__(self, reports_root: Path, assets_root: Path) -> None:
        """Bind the dedicated runtime reports root."""
        self._reports_root: Path = reports_root
        self._assets_root: Path = assets_root

    def write(self, plan: PortfolioPlan, inputs: PortfolioPlanningInputs) -> str:
        """Render exact optimizer inputs without executing active content."""
        report_id = plan.plan_hash
        positions = inputs.optimization_input.current_weights
        target = plan.payload.target_weights
        sectors: dict[str, float] = {}
        factors: dict[str, float] = {}
        for instrument, weight in target.items():
            sector = inputs.optimization_input.sectors[instrument]
            sectors[sector] = sectors.get(sector, 0.0) + weight
            for factor, loading in inputs.optimization_input.factor_loadings.get(instrument, {}).items():
                factors[factor] = factors.get(factor, 0.0) + weight * loading
        risk = {
            instrument: target[instrument]
            * sum(inputs.optimization_input.covariance[instrument][other] * target[other] for other in target)
            for instrument in target
        }
        review = PortfolioReview(
            title="Portfolio decision review",
            plan=plan,
            current_weights=positions,
            risk_contributions=risk,
            sector_exposures=sectors,
            factor_exposures=factors,
            thesis_statuses={item.instrument: item.action_tier.value for item in inputs.eligibility},
            conflicting_claims=inputs.report_conflicting_claims,
            source_authority_ratio_by_claim_category=(inputs.report_source_authority_ratio_by_claim_category),
            evidence=inputs.report_evidence,
            scenarios=inputs.report_scenarios,
            decision_diagnostics=(
                {
                    "expected_return_change": plan.payload.expected_return_change,
                    "expected_risk_change": plan.payload.expected_risk_change,
                    "turnover_estimate": plan.payload.turnover_estimate,
                },
            ),
            approval_state="pending",
            execution_state="not_started",
        )
        thumbnail_sources = {
            evidence.local_thumbnail: self._assets_root
            / evidence.local_thumbnail.stem[:2]
            / evidence.local_thumbnail.stem
            for evidence in review.evidence
            if evidence.local_thumbnail is not None
        }
        _ = write_report_bundle(
            review,
            self._reports_root / report_id,
            thumbnail_sources=thumbnail_sources,
        )
        return report_id
