"""Module composing evidence authority into immutable portfolio decisions."""

import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import ClassVar
from typing import Protocol

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue

from money_pit.portfolio.eligibility import ActionTier
from money_pit.portfolio.eligibility import EligibilityDecision
from money_pit.portfolio.eligibility import SupportedInstrumentKind
from money_pit.portfolio.optimizer import OptimizationInput
from money_pit.portfolio.optimizer import OptimizerBackend
from money_pit.portfolio.policy import PortfolioPolicy
from money_pit.portfolio.providers import LiquiditySnapshot
from money_pit.portfolio.providers import RiskSnapshot
from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.portfolio.tax_lots import InsufficientTaxLotsError
from money_pit.portfolio.tax_lots import SelectedTaxLot
from money_pit.portfolio.tax_lots import select_tax_lots
from money_pit.reports.portfolio import ReportEvidence
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.portfolio_plan import PlannedLotSelection
from money_pit.schemas.portfolio_plan import PlannedTaxLot
from money_pit.schemas.portfolio_plan import PlanTaxEstimate
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade
from money_pit.schemas.portfolio_plan import RejectedCandidate
from money_pit.schemas.portfolio_plan import UnresolvedLotSelection
from money_pit.schemas.snapshots import DecisionSnapshot
from money_pit.schemas.snapshots import DecisionSnapshotPayload
from money_pit.schemas.snapshots import SnapshotBinding
from money_pit.schemas.tax import LotSelectionPolicy
from money_pit.schemas.tax import TaxLotSnapshot
from money_pit.schemas.tax import WashSaleStatus


class PlanningInputError(Exception):
    """Raised when a portfolio review lacks exact eligibility or liquidity coverage."""


class PortfolioReviewRequest(BaseModel):
    """Stable harness input for a point-in-time A5 portfolio review."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    requested_as_of: AwareDatetime
    same_run_observation_ids: tuple[str, ...] = ()
    same_run_verification_result_ids: tuple[str, ...] = ()
    same_run_resolution_decision_ids: tuple[str, ...] = ()
    same_run_thesis_revision_ids: tuple[str, ...] = ()
    execution_eligible: bool
    source_id: str | None = Field(default=None, min_length=1)


class PortfolioReviewResult(BaseModel):
    """Durable references returned to the harness by A5."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    decision_snapshot_id: str = Field(min_length=1)
    decision_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_id: str | None = Field(default=None, min_length=1)
    outcome_schedule_ids: tuple[str, ...] = ()
    decision_at: AwareDatetime


class PortfolioPlanningService(Protocol):
    """Injected A5 boundary with no broker-write capability."""

    def review(self, request: PortfolioReviewRequest) -> PortfolioReviewResult:
        """Persist one decision, plan, and optional static report."""
        ...


class PortfolioPlanningInputs(BaseModel):
    """Complete immutable inputs supplied to deterministic A5 planning."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    portfolio_snapshot: PortfolioStateSnapshot
    market_snapshot: MarketStateSnapshot
    risk_snapshot: RiskSnapshot
    liquidity_snapshot: LiquiditySnapshot
    tax_snapshot: TaxLotSnapshot
    optimization_input: OptimizationInput
    eligibility: tuple[EligibilityDecision, ...]
    instrument_kinds: dict[str, SupportedInstrumentKind]
    report_conflicting_claims: tuple[str, ...] = ()
    report_source_authority_ratio_by_claim_category: dict[str, float] = Field(default_factory=dict)
    report_scenarios: tuple[dict[str, JsonValue], ...] = ()
    report_evidence: tuple[ReportEvidence, ...] = ()
    liquidity_maximum_weights: dict[str, float]
    evidence_fragment_ids: tuple[str, ...]
    claim_observation_ids: tuple[str, ...]
    verification_result_ids: tuple[str, ...]
    thesis_revision_ids: tuple[str, ...]
    canonical_projection_hashes: dict[str, str]
    claim_freshness_policy_version: str = Field(min_length=1)
    universe_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    processor_versions: dict[str, str]
    calibration_version: str = Field(min_length=1)
    optimizer_version: str = Field(min_length=1)
    trade_generation_version: str = Field(min_length=1)
    source_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_policy_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_policy_version: str | None = Field(default=None, min_length=1)
    model_versions: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)


class PortfolioPlanningInputProvider(Protocol):
    """Read-only boundary supplying exact point-in-time planning inputs."""

    def load(self, request: PortfolioReviewRequest) -> PortfolioPlanningInputs:
        """Return inputs whose capture times do not exceed the requested cutoff."""
        ...


class DecisionWriter(Protocol):
    """Append-only decision persistence boundary."""

    def append(self, snapshot: DecisionSnapshot) -> None:
        """Persist one immutable decision snapshot."""
        ...


class PlanWriter(Protocol):
    """Append-only plan persistence boundary."""

    def append(self, plan: PortfolioPlan) -> None:
        """Persist one immutable plan."""
        ...


class SnapshotWriter(Protocol):
    """Append-only persistence for every decision input snapshot."""

    def append_portfolio(self, snapshot: PortfolioStateSnapshot) -> None:
        """Persist portfolio state."""
        ...

    def append_market(self, snapshot: MarketStateSnapshot) -> None:
        """Persist market state."""
        ...

    def append_risk(self, snapshot: RiskSnapshot) -> None:
        """Persist risk state."""
        ...

    def append_liquidity(self, snapshot: LiquiditySnapshot) -> None:
        """Persist liquidity state."""
        ...

    def append_tax(self, snapshot: TaxLotSnapshot) -> None:
        """Persist tax-lot state."""
        ...


class PortfolioReportWriter(Protocol):
    """Static report bundle boundary invoked after exact plan persistence."""

    def write(self, plan: PortfolioPlan, inputs: PortfolioPlanningInputs) -> str:
        """Persist a report bundle and return its immutable identifier."""
        ...


class OutcomeScheduler(Protocol):
    """Append-only boundary for exact plan-bound future observations."""

    def schedule(self, plan: PortfolioPlan, thesis_revision_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Persist all configured outcome boundaries and return their identities."""
        ...


class DeterministicPortfolioPlanningService:
    """Build and persist one optimizer-derived, exact-hash portfolio plan."""

    def __init__(
        self,
        *,
        inputs: PortfolioPlanningInputProvider,
        optimizer: OptimizerBackend,
        policy: PortfolioPolicy,
        snapshots: SnapshotWriter,
        decisions: DecisionWriter,
        plans: PlanWriter,
        reports: PortfolioReportWriter,
        outcomes: OutcomeScheduler,
        plan_ttl: timedelta,
        implementation_version: str,
        tax_lot_policy: LotSelectionPolicy,
        minimum_trade_notional: float,
        maximum_slippage_bps: float,
        specific_tax_lot_ids: dict[str, tuple[str, ...]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        """Bind read-only inputs and append-only outputs; accept no broker capability."""
        if plan_ttl <= timedelta(0):
            raise ValueError("plan_ttl must be positive")
        if minimum_trade_notional <= 0 or maximum_slippage_bps < 0:
            raise ValueError("trade and slippage limits must be non-negative and trade minimum positive")
        self._inputs: PortfolioPlanningInputProvider = inputs
        self._optimizer: OptimizerBackend = optimizer
        self._policy: PortfolioPolicy = policy
        self._snapshots: SnapshotWriter = snapshots
        self._decisions: DecisionWriter = decisions
        self._plans: PlanWriter = plans
        self._reports: PortfolioReportWriter = reports
        self._outcomes: OutcomeScheduler = outcomes
        self._plan_ttl: timedelta = plan_ttl
        self._implementation_version: str = implementation_version
        self._tax_lot_policy: LotSelectionPolicy = tax_lot_policy
        self._minimum_trade_notional: float = minimum_trade_notional
        self._maximum_slippage_bps: float = maximum_slippage_bps
        self._specific_tax_lot_ids: dict[str, tuple[str, ...]] = (
            {} if specific_tax_lot_ids is None else dict(specific_tax_lot_ids)
        )
        self._clock: Callable[[], datetime] = clock

    def review(self, request: PortfolioReviewRequest) -> PortfolioReviewResult:
        """Persist an exact decision and deterministic long-only plan."""
        inputs: PortfolioPlanningInputs = self._inputs.load(request)
        decision_at: datetime = self._clock()
        _require_point_in_time_inputs(inputs, as_of=decision_at)
        created_at: datetime = self._clock()
        if created_at < decision_at or decision_at < request.requested_as_of:
            raise PlanningInputError("decision timestamps are not monotonic")
        self._snapshots.append_portfolio(inputs.portfolio_snapshot)
        self._snapshots.append_market(inputs.market_snapshot)
        self._snapshots.append_risk(inputs.risk_snapshot)
        self._snapshots.append_liquidity(inputs.liquidity_snapshot)
        self._snapshots.append_tax(inputs.tax_snapshot)
        constrained: OptimizationInput = constrain_optimization_input(
            inputs.optimization_input,
            eligibility=inputs.eligibility,
            liquidity_maximum_weights=inputs.liquidity_maximum_weights,
        )
        result = self._optimizer.optimize(constrained, self._policy)
        _require_planning_result_coverage(inputs, result.target_weights)
        decision_id: str = _stable_id("decision", request.run_id, decision_at, self._implementation_version)
        decision = DecisionSnapshot.from_payload(
            decision_id,
            DecisionSnapshotPayload(
                run_id=request.run_id,
                requested_as_of=request.requested_as_of,
                decision_at=decision_at,
                known_at=created_at,
                evidence_fragment_ids=tuple(sorted(set(inputs.evidence_fragment_ids))),
                claim_observation_ids=tuple(sorted(set(inputs.claim_observation_ids))),
                verification_result_ids=tuple(sorted(set(inputs.verification_result_ids))),
                thesis_revision_ids=tuple(sorted(set(inputs.thesis_revision_ids))),
                canonical_projection_hashes=inputs.canonical_projection_hashes,
                portfolio_snapshot=SnapshotBinding(
                    snapshot_id=inputs.portfolio_snapshot.snapshot_id,
                    captured_at=inputs.portfolio_snapshot.payload.captured_at,
                ),
                market_snapshot=SnapshotBinding(
                    snapshot_id=inputs.market_snapshot.snapshot_id,
                    captured_at=inputs.market_snapshot.payload.captured_at,
                ),
                risk_snapshot=SnapshotBinding(
                    snapshot_id=inputs.risk_snapshot.snapshot_id,
                    captured_at=inputs.risk_snapshot.payload.captured_at,
                ),
                liquidity_snapshot=SnapshotBinding(
                    snapshot_id=inputs.liquidity_snapshot.snapshot_id,
                    captured_at=inputs.liquidity_snapshot.payload.captured_at,
                ),
                tax_snapshot=SnapshotBinding(
                    snapshot_id=inputs.tax_snapshot.snapshot_id,
                    captured_at=inputs.tax_snapshot.captured_at,
                ),
                source_config_hash=inputs.source_config_hash,
                strategy_config_hash=inputs.strategy_config_hash,
                execution_config_hash=inputs.execution_config_hash,
                policy_version=self._policy.policy_version,
                claim_freshness_policy_version=inputs.claim_freshness_policy_version,
                universe_fingerprint=inputs.universe_fingerprint,
                processor_versions=inputs.processor_versions,
                calibration_version=inputs.calibration_version,
                optimizer_version=inputs.optimizer_version,
                trade_generation_version=inputs.trade_generation_version,
                execution_eligible=request.execution_eligible,
                model_versions=inputs.model_versions,
                prompt_versions=inputs.prompt_versions,
                additional_inputs={
                    "optimizer": result.model_dump(mode="json"),
                    "eligibility": [item.model_dump(mode="json") for item in inputs.eligibility],
                },
            ),
        )
        plan_id: str = _stable_id("plan", request.run_id, decision_at, decision.decision_hash)
        trades: tuple[ProposedTrade, ...] = _proposed_trades(
            inputs,
            result.target_weights,
            tax_lot_policy=self._tax_lot_policy,
            minimum_trade_notional=self._minimum_trade_notional,
            maximum_slippage_bps=self._maximum_slippage_bps,
            as_of=decision_at,
            specific_tax_lot_ids=self._specific_tax_lot_ids,
        )
        decisions_by_instrument: dict[str, EligibilityDecision] = {item.instrument: item for item in inputs.eligibility}
        rejected: tuple[RejectedCandidate, ...] = tuple(
            RejectedCandidate(instrument=name, reasons=decision.reasons)
            for name, decision in sorted(decisions_by_instrument.items())
            if decision.action_tier is not ActionTier.NEW_EXPOSURE and decision.reasons
        )
        plan = PortfolioPlan.from_payload(
            PortfolioPlanPayload(
                plan_id=plan_id,
                created_at=created_at,
                expires_at=created_at + self._plan_ttl,
                portfolio_snapshot_id=inputs.portfolio_snapshot.snapshot_id,
                market_snapshot_id=inputs.market_snapshot.snapshot_id,
                decision_snapshot_id=decision.decision_snapshot_id,
                decision_snapshot_hash=decision.decision_hash,
                account_id=inputs.portfolio_snapshot.payload.account_id,
                broker_environment=inputs.portfolio_snapshot.payload.broker_environment,
                execution_config_hash=inputs.execution_config_hash,
                execution_policy_hash=inputs.execution_policy_hash,
                execution_policy_version=inputs.execution_policy_version,
                policy_version=self._policy.policy_version,
                model_versions=inputs.model_versions,
                prompt_versions=inputs.prompt_versions,
                target_weights=result.target_weights,
                proposed_trades=trades,
                rejected_candidates=rejected,
                expected_risk_change=_expected_risk_delta(inputs.optimization_input, result.target_weights),
                expected_return_change=_expected_return_delta(inputs.optimization_input, result.target_weights),
                turnover_estimate=result.turnover,
                tax_estimate=_plan_tax_estimate(trades),
                evidence_gate_results=_evidence_gate_results(trades, decisions_by_instrument),
                constraint_results=_constraint_results(
                    result.target_weights,
                    result.turnover,
                    self._policy,
                    maximum_weights=constrained.maximum_weights,
                ),
                historical_non_executable=not request.execution_eligible,
            )
        )
        self._decisions.append(decision)
        self._plans.append(plan)
        outcome_schedule_ids = self._outcomes.schedule(plan, inputs.thesis_revision_ids)
        report_id = self._reports.write(plan, inputs)
        return PortfolioReviewResult(
            decision_snapshot_id=decision.decision_snapshot_id,
            decision_snapshot_hash=decision.decision_hash,
            plan_id=plan.payload.plan_id,
            plan_hash=plan.plan_hash,
            report_id=report_id,
            outcome_schedule_ids=outcome_schedule_ids,
            decision_at=decision_at,
        )


def _require_point_in_time_inputs(inputs: PortfolioPlanningInputs, *, as_of: datetime) -> None:
    captured_at: tuple[datetime, ...] = (
        inputs.portfolio_snapshot.payload.captured_at,
        inputs.market_snapshot.payload.captured_at,
        inputs.risk_snapshot.payload.captured_at,
        inputs.liquidity_snapshot.payload.captured_at,
        inputs.tax_snapshot.captured_at,
    )
    if any(value > as_of for value in captured_at):
        raise PlanningInputError("planning input was captured after the requested as-of time")
    expected_ids: tuple[tuple[str, str], ...] = (
        (inputs.optimization_input.portfolio_snapshot_id, inputs.portfolio_snapshot.snapshot_id),
        (inputs.optimization_input.market_snapshot_id, inputs.market_snapshot.snapshot_id),
    )
    if any(actual != expected for actual, expected in expected_ids):
        raise PlanningInputError("optimization inputs are bound to different snapshots")
    optimization_instruments = set(inputs.optimization_input.expected_returns)
    quote_instruments = {quote.instrument for quote in inputs.market_snapshot.payload.quotes}
    if not optimization_instruments <= quote_instruments:
        missing = sorted(optimization_instruments - quote_instruments)
        raise PlanningInputError(f"market snapshot lacks optimization quotes: {missing}")


def _require_planning_result_coverage(
    inputs: PortfolioPlanningInputs,
    target_weights: dict[str, float],
) -> None:
    """Bind optimizer output to its exact instruments and required trade metadata."""
    optimization_instruments = set(inputs.optimization_input.expected_returns)
    target_instruments = set(target_weights)
    if target_instruments != optimization_instruments:
        raise PlanningInputError("target weights must exactly cover optimization instruments")
    missing_kinds = sorted(target_instruments - set(inputs.instrument_kinds))
    if missing_kinds:
        raise PlanningInputError(f"instrument kinds do not cover target instruments: {missing_kinds}")


def _proposed_trades(
    inputs: PortfolioPlanningInputs,
    target_weights: dict[str, float],
    *,
    tax_lot_policy: LotSelectionPolicy,
    minimum_trade_notional: float,
    maximum_slippage_bps: float,
    as_of: datetime,
    specific_tax_lot_ids: dict[str, tuple[str, ...]],
) -> tuple[ProposedTrade, ...]:
    positions = {item.instrument: item for item in inputs.portfolio_snapshot.payload.positions}
    quotes = {item.instrument: item for item in inputs.market_snapshot.payload.quotes}
    instruments: set[str] = set(target_weights)
    missing_quotes = sorted(instruments - set(quotes))
    if missing_quotes:
        raise PlanningInputError(f"market snapshot lacks target quotes: {missing_quotes}")
    total_value: float = inputs.portfolio_snapshot.payload.available_cash + sum(
        item.market_value for item in positions.values()
    )
    if not math.isfinite(total_value) or total_value <= 0:
        raise PlanningInputError("portfolio value must be positive and finite")
    trades: list[ProposedTrade] = []
    tax_known: bool = (
        inputs.tax_snapshot.complete_for_known_accounts and not inputs.tax_snapshot.unknown_external_activity
    )
    liquidity = {item.instrument: item for item in inputs.liquidity_snapshot.payload.observations}
    for instrument in sorted(instruments):
        current_value: float = positions[instrument].market_value if instrument in positions else 0.0
        change_notional: float = target_weights[instrument] * total_value - current_value
        if math.isclose(change_notional, 0.0, abs_tol=1e-8) or abs(change_notional) < minimum_trade_notional:
            continue
        if liquidity[instrument].estimated_slippage_bps > maximum_slippage_bps:
            raise PlanningInputError(f"estimated slippage exceeds policy for {instrument}")
        price: float = quotes[instrument].price
        side: str = "buy" if change_notional > 0 else "sell"
        quantity: float = round(abs(change_notional) / price, 9)
        lot_selection: PlannedLotSelection | UnresolvedLotSelection | None = None
        if side == "sell":
            try:
                selection = select_tax_lots(
                    inputs.tax_snapshot,
                    instrument=instrument,
                    quantity=quantity,
                    sale_price=price,
                    policy=tax_lot_policy,
                    as_of=as_of,
                    specific_lot_ids=specific_tax_lot_ids.get(instrument, ()),
                )
                wash_status = inputs.tax_snapshot.wash_sale_status.get(instrument, WashSaleStatus.UNKNOWN)
                planned_lots: tuple[PlannedTaxLot, ...] = tuple(
                    _planned_tax_lot(item, inputs.tax_snapshot, as_of=as_of) for item in selection.selected_lots
                )
                costs_known = _tax_cost_is_known(selection.tax_cost_known, wash_status, planned_lots)
                lot_selection = PlannedLotSelection(
                    policy=selection.policy,
                    tax_snapshot_id=selection.snapshot_id,
                    lots=planned_lots,
                    estimated_gain=selection.estimated_gain,
                    tax_cost_known=costs_known,
                    wash_sale_status=wash_status,
                )
            except InsufficientTaxLotsError:
                lot_selection = UnresolvedLotSelection(
                    policy=tax_lot_policy,
                    tax_snapshot_id=inputs.tax_snapshot.snapshot_id,
                    reason="configured tax lots do not cover the proposed sale quantity",
                )
        trades.append(
            ProposedTrade(
                instrument=instrument,
                asset_class=_trade_asset_class(inputs.instrument_kinds[instrument]),
                side=side,
                quantity=quantity,
                estimated_notional=abs(change_notional),
                tax_cost_known=side == "buy"
                or (tax_known and isinstance(lot_selection, PlannedLotSelection) and lot_selection.tax_cost_known),
                lot_selection=lot_selection,
            )
        )
    return tuple(trades)


def _planned_tax_lot(item: SelectedTaxLot, snapshot: TaxLotSnapshot, *, as_of: datetime) -> PlannedTaxLot:
    long_term = as_of - item.acquired_at > timedelta(days=365)
    rate = snapshot.long_term_tax_rate if long_term else snapshot.short_term_tax_rate
    estimated_cost = None if rate is None else max(0.0, item.estimated_gain) * rate
    return PlannedTaxLot(
        lot_id=item.lot_id,
        quantity=item.quantity,
        unit_cost=item.unit_cost,
        estimated_gain=item.estimated_gain,
        acquired_at=item.acquired_at,
        long_term=long_term,
        estimated_tax_cost=estimated_cost,
    )


def _tax_cost_is_known(
    selection_complete: bool,
    wash_status: WashSaleStatus,
    planned_lots: tuple[PlannedTaxLot, ...],
) -> bool:
    """Return whether rates, holding periods, wash status, and lots are exact."""
    return (
        selection_complete
        and wash_status is WashSaleStatus.CLEAR
        and all(item.estimated_tax_cost is not None for item in planned_lots)
    )


def _plan_tax_estimate(trades: tuple[ProposedTrade, ...]) -> PlanTaxEstimate:
    sell_selections = tuple(trade.lot_selection for trade in trades if trade.side == "sell")
    if any(
        not isinstance(selection, PlannedLotSelection) or not selection.tax_cost_known for selection in sell_selections
    ):
        return PlanTaxEstimate(currency="USD", estimated_cost=None, known=False)
    cost = sum(
        lot.estimated_tax_cost or 0.0
        for selection in sell_selections
        if isinstance(selection, PlannedLotSelection)
        for lot in selection.lots
    )
    return PlanTaxEstimate(currency="USD", estimated_cost=cost, known=True)


def _trade_asset_class(kind: SupportedInstrumentKind) -> TradableAssetClass:
    """Return a capital-eligible authoritative asset class or fail closed."""
    if kind is SupportedInstrumentKind.US_EQUITY:
        return TradableAssetClass.US_EQUITY
    if kind is SupportedInstrumentKind.US_ETF:
        return TradableAssetClass.US_ETF
    raise PlanningInputError("a proposed trade has no authoritative supported asset class")


def _expected_return_delta(inputs: OptimizationInput, targets: dict[str, float]) -> float:
    return sum(
        (targets[instrument] - inputs.current_weights[instrument]) * inputs.expected_returns[instrument]
        for instrument in targets
    )


def _portfolio_variance(weights: dict[str, float], covariance: dict[str, dict[str, float]]) -> float:
    return sum(
        left_weight * right_weight * covariance[left][right]
        for left, left_weight in weights.items()
        for right, right_weight in weights.items()
    )


def _expected_risk_delta(inputs: OptimizationInput, targets: dict[str, float]) -> float:
    return _portfolio_variance(targets, inputs.covariance) - _portfolio_variance(
        inputs.current_weights, inputs.covariance
    )


def _evidence_gate_results(
    trades: tuple[ProposedTrade, ...],
    decisions: dict[str, EligibilityDecision],
) -> dict[str, bool]:
    buy_instruments: tuple[str, ...] = tuple(trade.instrument for trade in trades if trade.side == "buy")
    if not buy_instruments:
        return {"no_new_exposure": True}
    return {
        f"new_exposure:{instrument}": decisions[instrument].action_tier is ActionTier.NEW_EXPOSURE
        for instrument in buy_instruments
    }


def _constraint_results(
    target_weights: dict[str, float],
    turnover: float,
    policy: PortfolioPolicy,
    *,
    maximum_weights: dict[str, float],
) -> dict[str, bool]:
    return {
        "long_only": all(weight >= 0 for weight in target_weights.values()),
        "cash_minimum": sum(target_weights.values()) <= 1 - policy.minimum_cash_weight + policy.feasibility_tolerance,
        "turnover": turnover <= policy.maximum_turnover + policy.feasibility_tolerance,
        "position_limits": all(
            weight <= maximum_weights[instrument] + policy.feasibility_tolerance
            for instrument, weight in target_weights.items()
        ),
    }


def _stable_id(namespace: str, run_id: str, as_of: datetime, binding: str) -> str:
    payload: str = json.dumps(
        {"namespace": namespace, "run_id": run_id, "as_of": as_of.isoformat(), "binding": binding},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def constrain_optimization_input(
    optimization_input: OptimizationInput,
    *,
    eligibility: tuple[EligibilityDecision, ...],
    liquidity_maximum_weights: dict[str, float],
) -> OptimizationInput:
    """Prevent ineligible or illiquid candidates from gaining exposure."""
    instruments: set[str] = set(optimization_input.expected_returns)
    decisions: dict[str, EligibilityDecision] = {item.instrument: item for item in eligibility}
    if len(decisions) != len(eligibility) or not instruments <= set(decisions):
        raise PlanningInputError("eligibility decisions must cover every optimization instrument")
    if set(liquidity_maximum_weights) != instruments:
        raise PlanningInputError("liquidity limits must exactly cover optimization instruments")
    maximum_weights: dict[str, float] = {}
    expected_returns: dict[str, float] = dict(optimization_input.expected_returns)
    for instrument in sorted(instruments):
        liquidity_limit: float = liquidity_maximum_weights[instrument]
        if not math.isfinite(liquidity_limit) or liquidity_limit < 0 or liquidity_limit > 1:
            raise PlanningInputError("liquidity maximum weights must be between zero and one")
        decision: EligibilityDecision = decisions[instrument]
        current_weight: float = optimization_input.current_weights[instrument]
        if decision.action_tier is ActionTier.NEW_EXPOSURE:
            authority_limit: float = 1.0
        elif decision.action_tier is ActionTier.HOLD_OR_REDUCE:
            authority_limit = current_weight
            expected_returns[instrument] = min(0.0, expected_returns[instrument])
        else:
            authority_limit = 0.0
            expected_returns[instrument] = min(0.0, expected_returns[instrument])
        configured_limit = optimization_input.maximum_weights.get(instrument, 1.0)
        maximum_weights[instrument] = max(current_weight, min(authority_limit, liquidity_limit, configured_limit))
    return optimization_input.model_copy(
        update={
            "expected_returns": expected_returns,
            "maximum_weights": maximum_weights,
        }
    )
