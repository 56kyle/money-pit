from datetime import UTC
from datetime import datetime

import pytest

from money_pit.contracts import CandidateThesisDraft
from money_pit.pipeline.discovery import materialize_candidate
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.schemas.universe import UniverseLayer


def test_materialize_candidate_records_actual_decision_time_not_historical_cutoff() -> None:
    decision_at = datetime(2026, 8, 9, tzinfo=UTC)
    draft = CandidateThesisDraft(
        subject="New non-held instrument",
        direction=ThesisDirection.LONG,
        instrument="NEW",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
    )

    candidate = materialize_candidate(
        draft,
        visible_claim_keys=frozenset({"claim-1"}),
        universe_instruments=frozenset({"NEW"}),
        universe_origins=frozenset(),
        known_at=decision_at,
    )

    assert (candidate.created_at, candidate.known_at) == (decision_at, decision_at)


@pytest.mark.parametrize(
    "basis",
    [
        DiscoveryBasis(source_claim_keys=("claim-1",)),
        DiscoveryBasis(universe_layer=UniverseLayer.BENCHMARK, universe_reference="S&P 500:NEW"),
        DiscoveryBasis(universe_layer=UniverseLayer.QUANTITATIVE_SCREEN, universe_reference="quality:NEW"),
        DiscoveryBasis(universe_layer=UniverseLayer.PORTFOLIO_GAP, universe_reference="technology:NEW"),
    ],
    ids=("source-evidence", "benchmark", "configured-screen", "exposure-gap"),
)
def test_materialize_candidate_accepts_each_authorized_nonheld_discovery_route(
    basis: DiscoveryBasis,
) -> None:
    decision_at = datetime(2026, 8, 9, tzinfo=UTC)
    draft = CandidateThesisDraft(
        subject="New non-held instrument",
        direction=ThesisDirection.LONG,
        instrument="NEW",
        horizon_class=HorizonClass.TACTICAL,
        discovery_basis=basis,
    )

    candidate = materialize_candidate(
        draft,
        visible_claim_keys=frozenset({"claim-1"}),
        universe_instruments=frozenset({"NEW"}),
        universe_origins=frozenset(
            () if basis.universe_layer is None else ((basis.universe_layer, basis.universe_reference or ""),)
        ),
        known_at=decision_at,
    )

    assert candidate.discovery_basis == basis
