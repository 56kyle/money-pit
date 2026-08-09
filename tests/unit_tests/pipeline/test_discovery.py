from datetime import UTC
from datetime import datetime

import pytest

from money_pit.contracts import CandidateThesisDraft
from money_pit.contracts import ThesisRevisionDraft
from money_pit.pipeline.discovery import UnknownDiscoveryInstrumentError
from money_pit.pipeline.discovery import materialize_candidate
from money_pit.pipeline.synthesis import InvalidThesisLifecycleError
from money_pit.pipeline.synthesis import _resolve_revision_target  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.universe import LayeredUniverse
from money_pit.portfolio.universe import UniverseCandidate
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.theses import ScenarioOutcome
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.schemas.universe import UniverseLayer


_DECISION_AT = datetime(2026, 8, 9, tzinfo=UTC)
_CONFIGURED_UNIVERSE = LayeredUniverse(
    candidates=(
        UniverseCandidate(
            instrument="NEW",
            layers=(UniverseLayer.WATCHLIST,),
            references=("NEW", "S&P 500:NEW", "quality:NEW", "technology:NEW"),
        ),
    ),
    observation_only=(),
)


def _draft(*, instrument_reference: str, basis: DiscoveryBasis) -> CandidateThesisDraft:
    return CandidateThesisDraft(
        subject="New non-held instrument",
        direction=ThesisDirection.LONG,
        instrument_reference=instrument_reference,
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=basis,
    )


def test_materialize_candidate_records_actual_decision_time_not_historical_cutoff() -> None:
    candidate = materialize_candidate(
        _draft(
            instrument_reference="NEW",
            basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        ),
        visible_claim_keys=frozenset({"claim-1"}),
        universe=_CONFIGURED_UNIVERSE,
        universe_origins=frozenset(),
        source_grounded_references=frozenset({"new"}),
        known_at=_DECISION_AT,
    )

    assert (candidate.created_at, candidate.known_at) == (_DECISION_AT, _DECISION_AT)


@pytest.mark.parametrize(
    ("basis", "instrument_reference"),
    [
        pytest.param(DiscoveryBasis(source_claim_keys=("claim-1",)), "NEW", id="source-evidence"),
        pytest.param(
            DiscoveryBasis(universe_layer=UniverseLayer.BENCHMARK, universe_reference="S&P 500:NEW"),
            "S&P 500:NEW",
            id="benchmark",
        ),
        pytest.param(
            DiscoveryBasis(universe_layer=UniverseLayer.QUANTITATIVE_SCREEN, universe_reference="quality:NEW"),
            "quality:NEW",
            id="configured-screen",
        ),
        pytest.param(
            DiscoveryBasis(universe_layer=UniverseLayer.PORTFOLIO_GAP, universe_reference="technology:NEW"),
            "technology:NEW",
            id="exposure-gap",
        ),
    ],
)
def test_materialize_candidate_accepts_each_authorized_nonheld_discovery_route(
    basis: DiscoveryBasis,
    instrument_reference: str,
) -> None:
    candidate = materialize_candidate(
        _draft(instrument_reference=instrument_reference, basis=basis),
        visible_claim_keys=frozenset({"claim-1"}),
        universe=_CONFIGURED_UNIVERSE,
        universe_origins=frozenset(
            () if basis.universe_layer is None else ((basis.universe_layer, basis.universe_reference or ""),)
        ),
        source_grounded_references=frozenset({"new"}),
        known_at=_DECISION_AT,
    )

    assert (candidate.discovery_basis, candidate.instrument_reference, candidate.instrument) == (
        basis,
        instrument_reference,
        "NEW",
    )


def test_materialize_candidate_preserves_a_grounded_unknown_reference_for_research() -> None:
    candidate = materialize_candidate(
        _draft(
            instrument_reference="NOVEL",
            basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        ),
        visible_claim_keys=frozenset({"claim-1"}),
        universe=LayeredUniverse(candidates=(), observation_only=()),
        universe_origins=frozenset(),
        source_grounded_references=frozenset({"novel"}),
        known_at=_DECISION_AT,
    )

    assert (candidate.instrument_reference, candidate.instrument) == ("NOVEL", None)


def test_materialize_candidate_rejects_an_invented_ungrounded_reference() -> None:
    with pytest.raises(UnknownDiscoveryInstrumentError):
        _ = materialize_candidate(
            _draft(
                instrument_reference="INVENTED",
                basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
            ),
            visible_claim_keys=frozenset({"claim-1"}),
            universe=LayeredUniverse(candidates=(), observation_only=()),
            universe_origins=frozenset(),
            source_grounded_references=frozenset({"different-reference"}),
            known_at=_DECISION_AT,
        )


def test_materialize_candidate_resolves_a_configured_reference_without_changing_it() -> None:
    candidate = materialize_candidate(
        _draft(
            instrument_reference="new",
            basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        ),
        visible_claim_keys=frozenset({"claim-1"}),
        universe=_CONFIGURED_UNIVERSE,
        universe_origins=frozenset(),
        source_grounded_references=frozenset({"new"}),
        known_at=_DECISION_AT,
    )

    assert (candidate.instrument_reference, candidate.instrument) == ("new", "NEW")


def test__resolve_revision_target_rejects_promotion_of_an_unresolved_instrument_reference() -> None:
    candidate = materialize_candidate(
        _draft(
            instrument_reference="NOVEL",
            basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
        ),
        visible_claim_keys=frozenset({"claim-1"}),
        universe=LayeredUniverse(candidates=(), observation_only=()),
        universe_origins=frozenset(),
        source_grounded_references=frozenset({"novel"}),
        known_at=_DECISION_AT,
    )
    draft = ThesisRevisionDraft(
        promoted_from_candidate_id=candidate.candidate_thesis_id,
        subject=candidate.subject,
        instrument=None,
        direction=ThesisDirection.LONG,
        horizon_class=HorizonClass.TACTICAL,
        effective_from=_DECISION_AT,
        review_at=_DECISION_AT,
        scenario_distribution=(ScenarioOutcome(name="base", probability=1.0, expected_return=0.1),),
        invalidation_rules=("Evidence changes",),
        causal_mechanisms=("growth",),
        confidence=0.5,
        reasoning="Grounded reference remains unresolved.",
    )

    with pytest.raises(InvalidThesisLifecycleError):
        _ = _resolve_revision_target(draft, (candidate,), ())
