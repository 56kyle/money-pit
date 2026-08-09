from datetime import UTC
from datetime import datetime

import pytest

from money_pit.portfolio.eligibility import ActionTier
from money_pit.portfolio.eligibility import EligibilityDecision
from money_pit.portfolio.eligibility import SupportedInstrumentKind
from money_pit.portfolio.optimizer import OptimizationInput
from money_pit.portfolio.planning import PlanningInputError
from money_pit.portfolio.planning import PortfolioPlanningInputs
from money_pit.portfolio.planning import _require_planning_result_coverage  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.planning import _require_point_in_time_inputs  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.providers import InstrumentLiquidity
from money_pit.portfolio.providers import InstrumentRisk
from money_pit.portfolio.providers import LiquiditySnapshot
from money_pit.portfolio.providers import LiquiditySnapshotPayload
from money_pit.portfolio.providers import RiskSnapshot
from money_pit.portfolio.providers import RiskSnapshotPayload
from money_pit.portfolio.snapshots import MarketQuote
from money_pit.portfolio.snapshots import MarketStatePayload
from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.schemas.instrument import InstrumentExposureClass
from money_pit.schemas.tax import TaxLotSnapshot


_AS_OF = datetime(2026, 8, 9, tzinfo=UTC)


def _planning_inputs(*, quote_instruments: tuple[str, ...]) -> PortfolioPlanningInputs:
    portfolio = PortfolioStateSnapshot.from_payload(
        PortfolioStatePayload(
            account_id="paper-account",
            captured_at=_AS_OF,
            available_cash=1_000,
            positions=(),
            open_order_ids=(),
        )
    )
    market = MarketStateSnapshot.from_payload(
        MarketStatePayload(
            captured_at=_AS_OF,
            quotes=tuple(
                MarketQuote(
                    instrument=instrument,
                    price=100,
                    observed_at=_AS_OF,
                    source="offline-market",
                )
                for instrument in quote_instruments
            ),
        )
    )
    risk = RiskSnapshot.from_payload(
        RiskSnapshotPayload(
            captured_at=_AS_OF,
            observations=(
                InstrumentRisk(
                    instrument="AAPL",
                    sector="technology",
                    factor_loadings={},
                    covariance={"AAPL": 0.05},
                ),
            ),
        )
    )
    liquidity = LiquiditySnapshot.from_payload(
        LiquiditySnapshotPayload(
            captured_at=_AS_OF,
            observations=(
                InstrumentLiquidity(
                    instrument="AAPL",
                    average_daily_notional=100_000_000,
                    maximum_participation_rate=0.001,
                    estimated_slippage_bps=1,
                    tradable=True,
                ),
            ),
        )
    )
    optimization = OptimizationInput(
        portfolio_snapshot_id=portfolio.snapshot_id,
        market_snapshot_id=market.snapshot_id,
        current_weights={"AAPL": 0.0},
        expected_returns={"AAPL": 0.1},
        covariance={"AAPL": {"AAPL": 0.05}},
        sectors={"AAPL": "technology"},
        exposure_classes={"AAPL": InstrumentExposureClass.SINGLE_STOCK},
        tax_cost_per_sold_weight={"AAPL": 0.0},
        tax_cost_known={"AAPL": False},
        maximum_weights={"AAPL": 0.15},
    )
    return PortfolioPlanningInputs(
        portfolio_snapshot=portfolio,
        market_snapshot=market,
        risk_snapshot=risk,
        liquidity_snapshot=liquidity,
        tax_snapshot=TaxLotSnapshot(
            snapshot_id="tax-snapshot",
            captured_at=_AS_OF,
            lots=(),
            complete_for_known_accounts=False,
            unknown_external_activity=True,
        ),
        optimization_input=optimization,
        eligibility=(EligibilityDecision(instrument="AAPL", action_tier=ActionTier.NEW_EXPOSURE, reasons=()),),
        instrument_kinds={"AAPL": SupportedInstrumentKind.US_EQUITY},
        liquidity_maximum_weights={"AAPL": 0.15},
        evidence_fragment_ids=(),
        claim_observation_ids=(),
        verification_result_ids=(),
        thesis_revision_ids=(),
        canonical_projection_hashes={},
        claim_freshness_policy_version="freshness-1",
        universe_fingerprint="a" * 64,
        processor_versions={},
        calibration_version="calibration-1",
        optimizer_version="optimizer-1",
        trade_generation_version="trade-1",
        source_config_hash="b" * 64,
        strategy_config_hash="c" * 64,
    )


def test__require_point_in_time_inputs_accepts_market_quote_superset() -> None:
    _require_point_in_time_inputs(_planning_inputs(quote_instruments=("AAPL", "VTI")), as_of=_AS_OF)


def test__require_planning_result_coverage_accepts_exact_optimizer_targets() -> None:
    _require_planning_result_coverage(
        _planning_inputs(quote_instruments=("AAPL", "VTI")),
        {"AAPL": 0.1},
    )


def test__require_planning_result_coverage_rejects_a_missing_target() -> None:
    with pytest.raises(PlanningInputError):
        _require_planning_result_coverage(
            _planning_inputs(quote_instruments=("AAPL", "VTI")),
            {},
        )


def test__require_point_in_time_inputs_rejects_a_missing_optimization_quote() -> None:
    with pytest.raises(PlanningInputError):
        _require_point_in_time_inputs(_planning_inputs(quote_instruments=("VTI",)), as_of=_AS_OF)
