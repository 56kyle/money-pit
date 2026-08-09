"""Module assembling exact persistent intelligence and current state for A5."""

import hashlib
import importlib.metadata
import json
import sqlite3
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from typing import cast

from pydantic import HttpUrl
from pydantic import JsonValue
from pydantic import TypeAdapter

from money_pit.claims.repository import ClaimRepository
from money_pit.config import ReturnBoundPolicy
from money_pit.config import StrategyConfig
from money_pit.config import claim_refresh_policy
from money_pit.portfolio.calibration import ReturnCalibration
from money_pit.portfolio.calibration import ScenarioDistribution
from money_pit.portfolio.calibration import ScenarioEstimate
from money_pit.portfolio.calibration import calibrate_expected_return
from money_pit.portfolio.eligibility import CandidateAdmissionInput
from money_pit.portfolio.eligibility import EligibilityDecision
from money_pit.portfolio.eligibility import MaterialEvidenceAnchor
from money_pit.portfolio.eligibility import SupportedInstrumentKind
from money_pit.portfolio.eligibility import VerificationState
from money_pit.portfolio.eligibility import evaluate_candidate_eligibility
from money_pit.portfolio.optimizer import OptimizationInput
from money_pit.portfolio.planning import PortfolioPlanningInputs
from money_pit.portfolio.planning import PortfolioReviewRequest
from money_pit.portfolio.providers import LiquidityStateProvider
from money_pit.portfolio.providers import MarketStateProvider
from money_pit.portfolio.providers import PortfolioStateProvider
from money_pit.portfolio.providers import RiskStateProvider
from money_pit.portfolio.providers import TaxLotStateProvider
from money_pit.portfolio.snapshots import PortfolioStatePosition
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.portfolio.theses import ThesisRepository
from money_pit.reports.portfolio import ReportEvidence
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.schemas.tax import TaxLotSnapshot
from money_pit.schemas.tax import WashSaleStatus
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.theses import ThesisRevision
from money_pit.schemas.theses import ThesisStatus
from money_pit.storage.database import Database


class InstrumentAuthority(Protocol):
    """Broker-authoritative supported asset class and tradability lookup."""

    def instrument_authority(self, instrument: str) -> tuple[SupportedInstrumentKind, bool]:
        """Return supported kind and current tradability."""
        ...


class HistoricalPlanningRequiresReplayError(Exception):
    """Raised when current providers are requested for a historical decision."""


class PersistentPortfolioPlanningInputProvider:
    """Assemble exact A5 inputs from durable intelligence and current read providers."""

    def __init__(
        self,
        *,
        database: Database,
        strategy: StrategyConfig,
        portfolio: PortfolioStateProvider,
        market: MarketStateProvider,
        risk: RiskStateProvider,
        liquidity: LiquidityStateProvider,
        tax_lots: TaxLotStateProvider,
        instruments: InstrumentAuthority,
        source_config_hash: str,
        strategy_config_hash: str,
        execution_config_hash: str | None,
        execution_policy_hash: str | None,
        execution_policy_version: str | None,
        processor_versions: dict[str, str],
        model_versions: dict[str, str],
        prompt_versions: dict[str, str],
        trade_generation_version: str,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        """Bind persistent readers and current-state providers only."""
        self._database: Database = database
        self._strategy: StrategyConfig = strategy
        self._portfolio: PortfolioStateProvider = portfolio
        self._market: MarketStateProvider = market
        self._risk: RiskStateProvider = risk
        self._liquidity: LiquidityStateProvider = liquidity
        self._tax_lots: TaxLotStateProvider = tax_lots
        self._instruments: InstrumentAuthority = instruments
        self._source_config_hash: str = source_config_hash
        self._strategy_config_hash: str = strategy_config_hash
        self._execution_config_hash: str | None = execution_config_hash
        self._execution_policy_hash: str | None = execution_policy_hash
        self._execution_policy_version: str | None = execution_policy_version
        self._processor_versions: dict[str, str] = dict(processor_versions)
        self._model_versions: dict[str, str] = dict(model_versions)
        self._prompt_versions: dict[str, str] = dict(prompt_versions)
        self._trade_generation_version: str = trade_generation_version
        self._clock: Callable[[], datetime] = clock

    def load(self, request: PortfolioReviewRequest) -> PortfolioPlanningInputs:
        """Capture current state and bind every durable input used by A5."""
        if not request.execution_eligible:
            raise HistoricalPlanningRequiresReplayError(
                "historical portfolio planning requires exact recorded snapshots and cannot call current providers"
            )
        portfolio = self._portfolio.snapshot()
        revisions = self._revisions(request)
        instruments = self._universe(portfolio, revisions)
        market = self._market.snapshot(instruments)
        risk = self._risk.snapshot(instruments)
        liquidity = self._liquidity.snapshot(instruments)
        tax = self._tax_lots.snapshot()
        input_known_at = self._clock()
        claims = ClaimRepository(self._database, refresh_policy=claim_refresh_policy(self._strategy))
        baseline_observations = claims.observations_as_of(as_of=request.requested_as_of)
        same_run_observations = claims.observations_by_ids(request.same_run_observation_ids)
        observations = tuple(
            {item.observation_id: item for item in (*baseline_observations, *same_run_observations)}.values()
        )
        if any(item.known_at > input_known_at for item in same_run_observations):
            raise ValueError("same-run claim observation is future-dated")
        projections = claims.projections_with_deltas(
            requested_as_of=request.requested_as_of,
            observation_ids=request.same_run_observation_ids,
            resolution_ids=request.same_run_resolution_decision_ids,
            verification_ids=request.same_run_verification_result_ids,
        )
        claim_key_by_observation_id: dict[str, str] = {
            observation_id: projection.canonical_claim_key
            for projection in projections
            for observation_id in projection.active_observation_ids
        }
        anchors_by_claim, verification_ids = self._anchors(
            observations,
            claim_key_by_observation_id=claim_key_by_observation_id,
            request=request,
        )
        total_value = portfolio.payload.available_cash + sum(item.market_value for item in portfolio.payload.positions)
        if total_value <= 0:
            raise ValueError("portfolio value must be positive")
        positions = {item.instrument: item for item in portfolio.payload.positions}
        risk_by_instrument = {item.instrument: item for item in risk.payload.observations}
        liquidity_by_instrument = {item.instrument: item for item in liquidity.payload.observations}
        revision_by_instrument = {
            revision.instrument: revision
            for revision in revisions
            if revision.instrument is not None and revision.status in {ThesisStatus.ACTIVE, ThesisStatus.WEAKENED}
        }
        current_weights: dict[str, float] = {
            instrument: positions[instrument].market_value / total_value if instrument in positions else 0.0
            for instrument in instruments
        }
        eligibility: list[EligibilityDecision] = []
        instrument_kinds: dict[str, SupportedInstrumentKind] = {}
        expected_returns: dict[str, float] = {}
        for instrument in instruments:
            revision = revision_by_instrument.get(instrument)
            material_claim_keys: frozenset[str] = frozenset(() if revision is None else revision.supporting_claim_keys)
            contradicting_claim_keys: frozenset[str] = frozenset(
                () if revision is None else revision.contradicting_claim_keys
            )
            instrument_anchors = _anchors_for_claim_keys(
                material_claim_keys,
                anchors_by_claim,
                known_at=input_known_at,
            )
            kind, tradable = self._instruments.instrument_authority(instrument)
            instrument_kinds[instrument] = kind
            scenarios = None if revision is None else _scenario_distribution(revision, self._strategy)
            decision = evaluate_candidate_eligibility(
                CandidateAdmissionInput(
                    instrument=instrument,
                    instrument_kind=kind,
                    held_weight=current_weights[instrument],
                    thesis_active=revision is not None and revision.status is ThesisStatus.ACTIVE,
                    thesis_is_bearish=revision is not None and revision.direction is ThesisDirection.BEARISH,
                    thesis_valid_until=None if revision is None else revision.valid_until,
                    invalidation_rules=() if revision is None else revision.invalidation_rules,
                    scenarios=scenarios,
                    material_anchors=instrument_anchors,
                    contradicting_anchors=_anchors_for_claim_keys(
                        contradicting_claim_keys,
                        anchors_by_claim,
                        known_at=input_known_at,
                    ),
                    market_data_current=instrument in {quote.instrument for quote in market.payload.quotes},
                    risk_data_current=instrument in risk_by_instrument,
                    liquidity_data_current=instrument in liquidity_by_instrument,
                    tradable=tradable,
                ),
                as_of=input_known_at,
            )
            eligibility.append(decision)
            expected_returns[instrument] = _expected_return(
                revision,
                scenarios,
                instrument_anchors,
                calibration_version=(
                    f"{self._strategy.expected_return_calibration_version}:"
                    f"{self._strategy.expected_return_bounds_version}"
                ),
                uncertainty_multiplier=self._strategy.expected_return_uncertainty_multiplier,
                calibrated_floor=self._strategy.calibrated_return_floor,
                calibrated_ceiling=self._strategy.calibrated_return_ceiling,
                bound_policy=self._strategy.return_bound_policy,
                as_of=input_known_at,
            )
        maximum_weights = {
            instrument: min(
                self._strategy.name_weight_limit,
                liquidity_by_instrument[instrument].average_daily_notional
                * liquidity_by_instrument[instrument].maximum_participation_rate
                / total_value,
            )
            for instrument in instruments
        }
        tax_cost_per_sold_weight, tax_cost_known = _tax_cost_inputs(
            tax,
            positions=positions,
            prices={quote.instrument: quote.price for quote in market.payload.quotes},
            as_of=input_known_at,
        )
        optimization = OptimizationInput(
            portfolio_snapshot_id=portfolio.snapshot_id,
            market_snapshot_id=market.snapshot_id,
            current_weights=current_weights,
            expected_returns=expected_returns,
            covariance={item.instrument: item.covariance for item in risk.payload.observations},
            sectors={item.instrument: item.sector for item in risk.payload.observations},
            satellite_instruments=frozenset(set(instruments) - set(self._strategy.strategic_core_targets)),
            tax_cost_per_sold_weight=tax_cost_per_sold_weight,
            tax_cost_known=tax_cost_known,
            maximum_weights=maximum_weights,
            factor_loadings={item.instrument: item.factor_loadings for item in risk.payload.observations},
            correlated_groups={
                name: frozenset(instruments) for name, instruments in self._strategy.correlated_exposure_groups.items()
            },
        )
        evidence_ids = tuple(
            sorted({item for observation in observations for item in observation.evidence_fragment_ids})
        )
        return PortfolioPlanningInputs(
            portfolio_snapshot=portfolio,
            market_snapshot=market,
            risk_snapshot=risk,
            liquidity_snapshot=liquidity,
            tax_snapshot=tax,
            optimization_input=optimization,
            eligibility=tuple(eligibility),
            instrument_kinds=instrument_kinds,
            report_conflicting_claims=tuple(
                sorted({claim for revision in revisions for claim in revision.contradicting_claim_keys})
            ),
            report_source_authority_ratio_by_claim_category=(
                self._source_authority_ratio_by_claim_category(observations)
            ),
            report_scenarios=tuple(
                {
                    "thesis_revision_id": revision.revision_id,
                    "instrument": revision.instrument,
                    "scenarios": [item.model_dump(mode="json") for item in revision.scenario_distribution],
                }
                for revision in revisions
            ),
            report_evidence=self._report_evidence(observations),
            liquidity_maximum_weights=maximum_weights,
            evidence_fragment_ids=evidence_ids,
            claim_observation_ids=tuple(item.observation_id for item in observations),
            verification_result_ids=verification_ids,
            thesis_revision_ids=tuple(item.revision_id for item in revisions),
            canonical_projection_hashes={item.canonical_claim_key: _hash_model(item) for item in projections},
            claim_freshness_policy_version=self._strategy.claim_freshness.version,
            universe_fingerprint=_hash_json({"instruments": instruments}),
            processor_versions=self._processor_versions,
            calibration_version=(
                f"{self._strategy.expected_return_calibration_version}:{self._strategy.expected_return_bounds_version}"
            ),
            optimizer_version=importlib.metadata.version("clarabel"),
            trade_generation_version=self._trade_generation_version,
            source_config_hash=self._source_config_hash,
            strategy_config_hash=self._strategy_config_hash,
            execution_config_hash=self._execution_config_hash,
            execution_policy_hash=self._execution_policy_hash,
            execution_policy_version=self._execution_policy_version,
            model_versions=self._model_versions,
            prompt_versions=self._prompt_versions,
        )

    def _source_authority_ratio_by_claim_category(
        self,
        observations: tuple[ClaimObservation, ...],
    ) -> dict[str, float]:
        counts: dict[str, tuple[int, int]] = {}
        with self._database.transaction() as connection:
            for observation in observations:
                source = _source_definition(connection, observation.source_item_id)
                authoritative = int(_authoritative(source, observation))
                accepted, total = counts.get(observation.category.value, (0, 0))
                counts[observation.category.value] = (accepted + authoritative, total + 1)
        return {category: accepted / total for category, (accepted, total) in counts.items()}

    def _report_evidence(self, observations: tuple[ClaimObservation, ...]) -> tuple[ReportEvidence, ...]:
        records: list[ReportEvidence] = []
        with self._database.transaction() as connection:
            for observation in observations:
                for fragment_id in observation.evidence_fragment_ids:
                    row: sqlite3.Row | None = cast(
                        "sqlite3.Row | None",
                        connection.execute(
                            """SELECT item.canonical_uri, item.source_item_id, fragment.locator_json,
                                  fragment.asset_id, fragment.fragment_kind
                        FROM evidence_fragments AS fragment
                        JOIN evidence_asset_acquisitions AS acquisition ON acquisition.asset_id = fragment.asset_id
                        JOIN source_items AS item ON item.source_item_id = acquisition.source_item_id
                          AND item.content_version = acquisition.content_version
                        WHERE fragment.fragment_id = ? AND item.source_item_id = ?
                        ORDER BY acquisition.retrieved_at DESC LIMIT 1""",
                            (fragment_id, observation.source_item_id),
                        ).fetchone(),
                    )
                    if row is None:
                        continue
                    canonical_uri = str(cast("object", row[0]))
                    locator: dict[str, JsonValue] = TypeAdapter(dict[str, JsonValue]).validate_json(
                        str(cast("object", row[2]))
                    )
                    web_url = _report_web_url(canonical_uri, locator)
                    asset_id = str(cast("object", row[3]))
                    is_frame = str(cast("object", row[4])) == "frame"
                    records.append(
                        ReportEvidence(
                            label=f"{observation.category.value} claim {observation.observation_id}",
                            web_url=None if web_url is None else HttpUrl(web_url),
                            local_thumbnail=(Path("thumbnails", f"{asset_id}.png") if is_frame else None),
                            unavailable_reason=(
                                None
                                if web_url is not None or is_frame
                                else "source has no safe web locator or report-local thumbnail"
                            ),
                            source_item_id=str(cast("object", row[1])),
                            status="durable evidence",
                        )
                    )
        return tuple(records)

    def _revisions(self, request: PortfolioReviewRequest) -> tuple[ThesisRevision, ...]:
        repository = ThesisRepository(self._database)
        latest_by_thesis: dict[str, ThesisRevision] = {
            item.thesis_id: item for item in repository.revisions_as_of(as_of=request.requested_as_of)
        }
        for revision in repository.revisions_by_ids(request.same_run_thesis_revision_ids):
            if revision.known_at > self._clock():
                raise ValueError("same-run thesis revision is absent or future-dated")
            current = latest_by_thesis.get(revision.thesis_id)
            if current is None or revision.revision_number > current.revision_number:
                latest_by_thesis[revision.thesis_id] = revision
        return tuple(sorted(latest_by_thesis.values(), key=lambda item: item.thesis_id))

    def _universe(
        self,
        portfolio: PortfolioStateSnapshot,
        revisions: tuple[ThesisRevision, ...],
    ) -> tuple[str, ...]:
        held: set[str] = {item.instrument for item in portfolio.payload.positions}
        configured = {
            *self._strategy.strategic_core_targets,
            *self._strategy.watchlist,
            *self._strategy.benchmark_constituents,
            *self._strategy.explicit_proxies.values(),
        }
        thesis_instruments = {item.instrument for item in revisions if item.instrument is not None}
        return tuple(sorted({item.strip().upper() for item in held | configured | thesis_instruments}))

    def _anchors(
        self,
        observations: tuple[ClaimObservation, ...],
        *,
        claim_key_by_observation_id: dict[str, str],
        request: PortfolioReviewRequest,
    ) -> tuple[dict[str, tuple[MaterialEvidenceAnchor, ...]], tuple[str, ...]]:
        anchors: dict[str, list[MaterialEvidenceAnchor]] = {}
        verification_ids: list[str] = []
        with self._database.transaction() as connection:
            for observation in observations:
                claim_key = claim_key_by_observation_id.get(observation.observation_id)
                if claim_key is None:
                    continue
                verification = _latest_verification(
                    connection,
                    observation.observation_id,
                    baseline_as_of=request.requested_as_of.isoformat(),
                    decision_at=self._clock().isoformat(),
                    same_run_ids=request.same_run_verification_result_ids,
                )
                if verification is not None:
                    verification_ids.append(verification.verification_id)
                authority = _verification_portfolio_authority(connection, observation, verification)
                anchor = MaterialEvidenceAnchor(
                    claim_key=claim_key,
                    status=_verification_state(verification),
                    known_at=(
                        observation.known_at
                        if verification is None
                        else max(observation.known_at, verification.known_at)
                    ),
                    valid_until=observation.valid_until if verification is None else verification.valid_until,
                    allowed_for_portfolio=authority.allowed_for_portfolio,
                    authoritative_primary=authority.authoritative_primary,
                    independent_provenance_groups=authority.independent_provenance_groups,
                )
                anchors.setdefault(claim_key, []).append(anchor)
        return (
            {instrument: tuple(items) for instrument, items in anchors.items()},
            tuple(sorted(set(verification_ids))),
        )


def _scenario_distribution(revision: ThesisRevision, strategy: StrategyConfig) -> ScenarioDistribution:
    values = tuple(item.expected_return for item in revision.scenario_distribution)
    if strategy.return_bound_policy is ReturnBoundPolicy.REJECT and any(
        value < strategy.scenario_return_floor or value > strategy.scenario_return_ceiling for value in values
    ):
        raise ValueError("scenario return is outside the configured calibration bounds")
    return ScenarioDistribution(
        scenarios=tuple(
            ScenarioEstimate(
                name=item.name,
                probability=item.probability,
                expected_return=min(
                    strategy.scenario_return_ceiling,
                    max(strategy.scenario_return_floor, item.expected_return),
                ),
            )
            for item in revision.scenario_distribution
        )
    )


def _anchors_for_claim_keys(
    claim_keys: frozenset[str],
    anchors_by_claim: dict[str, tuple[MaterialEvidenceAnchor, ...]],
    *,
    known_at: datetime,
) -> tuple[MaterialEvidenceAnchor, ...]:
    anchors: list[MaterialEvidenceAnchor] = []
    for claim_key in sorted(claim_keys):
        projected = anchors_by_claim.get(claim_key)
        if projected:
            anchors.extend(projected)
        else:
            anchors.append(
                MaterialEvidenceAnchor(
                    claim_key=claim_key,
                    status=VerificationState.UNRESOLVED,
                    known_at=known_at,
                    valid_until=None,
                    allowed_for_portfolio=False,
                    authoritative_primary=False,
                    independent_provenance_groups=(),
                )
            )
    return tuple(anchors)


def _expected_return(
    revision: ThesisRevision | None,
    scenarios: ScenarioDistribution | None,
    anchors: tuple[MaterialEvidenceAnchor, ...],
    *,
    calibration_version: str,
    uncertainty_multiplier: float,
    calibrated_floor: float,
    calibrated_ceiling: float,
    bound_policy: ReturnBoundPolicy,
    as_of: datetime,
) -> float:
    if revision is None or scenarios is None:
        return 0.0
    supported = bool(anchors) and all(item.status is VerificationState.SUPPORTED for item in anchors)
    fresh = all(item.valid_until is None or item.valid_until > as_of for item in anchors)
    calibrated = calibrate_expected_return(
        scenarios,
        ReturnCalibration(
            calibration_version=calibration_version,
            thesis_confidence=revision.confidence,
            verification_multiplier=1.0 if supported else 0.0,
            freshness_multiplier=1.0 if fresh else 0.0,
            uncertainty_multiplier=uncertainty_multiplier,
        ),
    ).calibrated_expected_return
    if bound_policy is ReturnBoundPolicy.REJECT and not calibrated_floor <= calibrated <= calibrated_ceiling:
        raise ValueError("calibrated return is outside the configured bounds")
    return min(calibrated_ceiling, max(calibrated_floor, calibrated))


def _latest_verification(
    connection: sqlite3.Connection,
    observation_id: str,
    *,
    baseline_as_of: str,
    decision_at: str,
    same_run_ids: tuple[str, ...],
) -> VerificationResult | None:
    placeholders = ",".join("?" for _ in same_run_ids)
    exact_clause = f" OR verification_id IN ({placeholders})" if placeholders else ""
    parameters: tuple[str, ...] = (observation_id, baseline_as_of, *same_run_ids, decision_at)
    # The optional clause contains generated placeholders only; all values are bound.
    query = (
        "SELECT verification_json FROM verification_results "  # noqa: S608  # nosec B608 -- dynamic text contains generated placeholders only.
        f"WHERE observation_id = ? AND (known_at <= ?{exact_clause}) AND known_at <= ? "
        "ORDER BY known_at DESC, checked_at DESC, verification_id DESC LIMIT 1"
    )
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute(query, parameters).fetchone(),
    )
    return (
        None if row is None else VerificationResult.model_validate_json(str(cast("object", row["verification_json"])))
    )


def _source_definition(connection: sqlite3.Connection, source_item_id: str) -> SourceDefinition:
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute(
            "SELECT revision.definition_json FROM source_items item JOIN source_definition_revisions revision ON revision.definition_hash = item.source_definition_hash WHERE item.source_item_id = ? ORDER BY item.discovered_at DESC LIMIT 1",
            (source_item_id,),
        ).fetchone(),
    )
    if row is None:
        raise ValueError("claim source definition is absent")
    return SourceDefinition.model_validate_json(str(cast("object", row["definition_json"])))


def _verification_state(value: VerificationResult | None) -> VerificationState:
    return VerificationState.UNRESOLVED if value is None else VerificationState(value.status.value)


@dataclass(frozen=True)
class _PortfolioEvidenceAuthority:
    allowed_for_portfolio: bool
    authoritative_primary: bool
    independent_provenance_groups: tuple[str, ...]


def _verification_portfolio_authority(
    connection: sqlite3.Connection,
    observation: ClaimObservation,
    verification: VerificationResult | None,
) -> _PortfolioEvidenceAuthority:
    if verification is None:
        return _PortfolioEvidenceAuthority(False, False, ())
    fragment_ids = _verification_authority_fragment_ids(verification)
    origin_groups = _observation_origin_groups(connection, observation)
    category = _anchor_trust_category(observation)
    authority_by_asset: dict[str, tuple[str, TrustLevel]] = {}
    for fragment_id in fragment_ids:
        rows: list[sqlite3.Row] = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """
                SELECT fragment.asset_id, definition.definition_json
                FROM evidence_fragments AS fragment
                JOIN evidence_asset_acquisitions AS acquisition
                  ON acquisition.asset_id = fragment.asset_id
                JOIN source_definition_revisions AS definition
                  ON definition.definition_hash = acquisition.source_definition_hash
                WHERE fragment.fragment_id = ?
                ORDER BY definition.provenance_group, definition.definition_hash
                """,
                (fragment_id,),
            ).fetchall(),
        )
        eligible: list[tuple[str, TrustLevel]] = []
        for row in rows:
            definition = SourceDefinition.model_validate_json(str(cast("object", row[1])))
            level = _trust_level(definition, category)
            if (
                AllowedUse.PORTFOLIO_DECISION in definition.allowed_uses
                and definition.provenance_group not in origin_groups
                and level in {TrustLevel.AUTHORITATIVE_PRIMARY, TrustLevel.INDEPENDENT_SECONDARY}
            ):
                eligible.append((definition.provenance_group, level))
        if rows and eligible:
            authority_by_asset[str(cast("object", rows[0][0]))] = min(
                eligible,
                key=lambda value: (value[1] is not TrustLevel.AUTHORITATIVE_PRIMARY, value[0]),
            )
    groups = tuple(sorted({group for group, _level in authority_by_asset.values()}))
    return _PortfolioEvidenceAuthority(
        allowed_for_portfolio=bool(authority_by_asset),
        authoritative_primary=any(
            level is TrustLevel.AUTHORITATIVE_PRIMARY for _group, level in authority_by_asset.values()
        ),
        independent_provenance_groups=groups,
    )


def _verification_authority_fragment_ids(verification: VerificationResult) -> tuple[str, ...]:
    if verification.status.value == VerificationState.SUPPORTED.value:
        return verification.supporting_evidence_ids
    if verification.status.value == VerificationState.CONTRADICTED.value:
        return verification.contradicting_evidence_ids
    return tuple(dict.fromkeys((*verification.supporting_evidence_ids, *verification.contradicting_evidence_ids)))


def _observation_origin_groups(
    connection: sqlite3.Connection,
    observation: ClaimObservation,
) -> frozenset[str]:
    groups: set[str] = set()
    for fragment_id in observation.evidence_fragment_ids:
        rows: list[sqlite3.Row] = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """
                SELECT definition.definition_json
                FROM evidence_fragments AS fragment
                JOIN evidence_asset_acquisitions AS acquisition
                  ON acquisition.asset_id = fragment.asset_id
                JOIN source_definition_revisions AS definition
                  ON definition.definition_hash = acquisition.source_definition_hash
                WHERE fragment.fragment_id = ? AND acquisition.source_item_id = ?
                """,
                (fragment_id, observation.source_item_id),
            ).fetchall(),
        )
        groups.update(
            SourceDefinition.model_validate_json(str(cast("object", row[0]))).provenance_group for row in rows
        )
    return frozenset(groups)


def _anchor_trust_category(observation: ClaimObservation) -> TrustCategory:
    if observation.category.value == TrustCategory.MARKET.value:
        return TrustCategory.MARKET
    if observation.category.value == TrustCategory.PORTFOLIO.value:
        return TrustCategory.PORTFOLIO
    return TrustCategory.FACTUAL


def _trust_level(source: SourceDefinition, category: TrustCategory) -> TrustLevel:
    return next(
        (setting.level for setting in source.trust_settings if setting.category is category),
        TrustLevel.UNTRUSTED,
    )


def _authoritative(source: SourceDefinition, observation: ClaimObservation) -> bool:
    category = TrustCategory(observation.claim_kind.value)
    return any(
        item.category is category and item.level is TrustLevel.AUTHORITATIVE_PRIMARY for item in source.trust_settings
    )


def _hash_model(value: CanonicalClaim) -> str:
    return _hash_json(value.model_dump(mode="json"))


def _hash_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _report_web_url(canonical_uri: str, locator: dict[str, JsonValue]) -> str | None:
    parsed = urllib.parse.urlsplit(canonical_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    fragment = parsed.fragment
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if locator.get("kind") == "timestamp":
        start = locator.get("start_seconds")
        if isinstance(start, int | float) and start >= 0:
            query = [(key, value) for key, value in query if key != "t"]
            query.append(("t", str(int(start))))
    elif locator.get("kind") == "page":
        page = locator.get("page_number")
        if isinstance(page, int) and page >= 1:
            fragment = f"page={page}"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), fragment))


def _tax_cost_known_for_instrument(snapshot: TaxLotSnapshot, instrument: str) -> bool:
    return (
        snapshot.complete_for_known_accounts
        and not snapshot.unknown_external_activity
        and snapshot.short_term_tax_rate is not None
        and snapshot.long_term_tax_rate is not None
        and snapshot.wash_sale_status.get(instrument) is WashSaleStatus.CLEAR
    )


def _tax_cost_inputs(
    snapshot: TaxLotSnapshot,
    *,
    positions: dict[str, PortfolioStatePosition],
    prices: dict[str, float],
    as_of: datetime,
) -> tuple[dict[str, float], dict[str, bool]]:
    costs: dict[str, float] = {}
    known: dict[str, bool] = {}
    for instrument, price in prices.items():
        position = positions.get(instrument)
        if position is None or position.market_value <= 0:
            costs[instrument] = 0.0
            known[instrument] = True
            continue
        if not _tax_cost_known_for_instrument(snapshot, instrument):
            costs[instrument] = 0.0
            known[instrument] = False
            continue
        covered_value = 0.0
        for lot in snapshot.lots:
            if lot.instrument.strip().upper() != instrument:
                continue
            covered_value += lot.quantity * price
        if covered_value + 0.01 < position.market_value:
            costs[instrument] = 0.0
            known[instrument] = False
        else:
            conservative_costs: list[float] = []
            for lot in snapshot.lots:
                if lot.instrument.strip().upper() != instrument:
                    continue
                rate = (
                    snapshot.long_term_tax_rate
                    if as_of - lot.acquired_at > timedelta(days=365)
                    else snapshot.short_term_tax_rate
                )
                if rate is None:
                    raise AssertionError("known tax input lost a required rate")
                conservative_costs.append(max(price - lot.unit_cost, 0.0) / price * rate)
            costs[instrument] = max(conservative_costs, default=0.0)
            known[instrument] = True
    return costs, known
