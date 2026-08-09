from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from money_pit.portfolio.calibration import ScenarioDistribution
from money_pit.portfolio.calibration import ScenarioEstimate
from money_pit.portfolio.eligibility import ActionTier
from money_pit.portfolio.eligibility import CandidateAdmissionInput
from money_pit.portfolio.eligibility import EligibilityDecision
from money_pit.portfolio.eligibility import MaterialEvidenceAnchor
from money_pit.portfolio.eligibility import SupportedInstrumentKind
from money_pit.portfolio.eligibility import VerificationState
from money_pit.portfolio.eligibility import evaluate_candidate_eligibility
from money_pit.portfolio.optimizer import OptimizationInput
from money_pit.portfolio.planning import constrain_optimization_input


_AS_OF = datetime(2026, 8, 1, tzinfo=UTC)


def _scenarios() -> ScenarioDistribution:
    return ScenarioDistribution(
        scenarios=(
            ScenarioEstimate(name="upside", probability=0.6, expected_return=0.2),
            ScenarioEstimate(name="downside", probability=0.4, expected_return=-0.1),
        ),
    )


def _candidate(anchor: MaterialEvidenceAnchor) -> CandidateAdmissionInput:
    return CandidateAdmissionInput(
        instrument="NEW",
        instrument_kind=SupportedInstrumentKind.US_EQUITY,
        held_weight=0.0,
        thesis_active=True,
        thesis_is_bearish=False,
        thesis_valid_until=_AS_OF + timedelta(days=30),
        invalidation_rules=("Primary evidence reverses",),
        scenarios=_scenarios(),
        material_anchors=(anchor,),
        market_data_current=True,
        risk_data_current=True,
        liquidity_data_current=True,
        tradable=True,
    )


def _anchor(
    *,
    status: VerificationState = VerificationState.SUPPORTED,
    valid_until: datetime | None = None,
    authoritative_primary: bool = True,
    provenance_groups: tuple[str, ...] = (),
    allowed_for_portfolio: bool = True,
) -> MaterialEvidenceAnchor:
    return MaterialEvidenceAnchor(
        claim_key="material-claim",
        status=status,
        known_at=_AS_OF,
        valid_until=valid_until or _AS_OF + timedelta(days=7),
        allowed_for_portfolio=allowed_for_portfolio,
        authoritative_primary=authoritative_primary,
        independent_provenance_groups=provenance_groups,
    )


def test_evaluate_candidate_eligibility_allows_a_supported_nonheld_instrument() -> None:
    decision = evaluate_candidate_eligibility(_candidate(_anchor()), as_of=_AS_OF)

    assert decision.action_tier is ActionTier.NEW_EXPOSURE


def test_evaluate_candidate_eligibility_does_not_count_syndicated_copies_as_independent() -> None:
    anchor = _anchor(authoritative_primary=False, provenance_groups=("upstream-wire",))

    decision = evaluate_candidate_eligibility(_candidate(anchor), as_of=_AS_OF)

    assert decision.action_tier is ActionTier.OBSERVATION_ONLY


@pytest.mark.parametrize("anchor", [pytest.param(_anchor(valid_until=_AS_OF), id="expired")])
def test_evaluate_candidate_eligibility_blocks_invalid_material_evidence(
    anchor: MaterialEvidenceAnchor,
) -> None:
    decision = evaluate_candidate_eligibility(_candidate(anchor), as_of=_AS_OF)

    assert decision.action_tier is ActionTier.OBSERVATION_ONLY


@pytest.mark.parametrize(
    "contradiction",
    [
        pytest.param(_anchor(status=VerificationState.SUPPORTED), id="supported"),
        pytest.param(_anchor(status=VerificationState.MIXED), id="mixed"),
        pytest.param(_anchor(status=VerificationState.UNRESOLVED), id="unresolved"),
        pytest.param(_anchor(status=VerificationState.SUPPORTED, valid_until=_AS_OF), id="stale"),
        pytest.param(
            _anchor(status=VerificationState.SUPPORTED, allowed_for_portfolio=False),
            id="unauthorized",
        ),
    ],
)
def test_evaluate_candidate_eligibility_blocks_unresolved_material_contradiction(
    contradiction: MaterialEvidenceAnchor,
) -> None:
    candidate = _candidate(_anchor()).model_copy(update={"contradicting_anchors": (contradiction,)})

    decision = evaluate_candidate_eligibility(candidate, as_of=_AS_OF)

    assert decision.action_tier is ActionTier.OBSERVATION_ONLY


def test_evaluate_candidate_eligibility_allows_disproved_material_contradiction() -> None:
    candidate = _candidate(_anchor()).model_copy(
        update={"contradicting_anchors": (_anchor(status=VerificationState.CONTRADICTED),)}
    )

    decision = evaluate_candidate_eligibility(candidate, as_of=_AS_OF)

    assert decision.action_tier is ActionTier.NEW_EXPOSURE


def test_constrain_optimization_input_with_material_contradiction_denies_buy(
    optimization_input: OptimizationInput,
) -> None:
    candidate = _candidate(_anchor()).model_copy(
        update={
            "instrument": "AAPL",
            "contradicting_anchors": (_anchor(status=VerificationState.SUPPORTED),),
        }
    )
    decision = evaluate_candidate_eligibility(candidate, as_of=_AS_OF)

    limited = constrain_optimization_input(
        optimization_input,
        eligibility=(
            decision,
            EligibilityDecision(instrument="SPY", action_tier=ActionTier.NEW_EXPOSURE, reasons=()),
            EligibilityDecision(instrument="XOM", action_tier=ActionTier.NEW_EXPOSURE, reasons=()),
        ),
        liquidity_maximum_weights={"AAPL": 1.0, "SPY": 1.0, "XOM": 1.0},
    )

    assert limited.maximum_weights["AAPL"] == 0.0
