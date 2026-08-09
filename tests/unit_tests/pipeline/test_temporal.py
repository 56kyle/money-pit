from datetime import UTC
from datetime import datetime
from datetime import timedelta

from money_pit.pipeline.temporal import temporal_compatibility
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.theses import ScenarioOutcome
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.theses import ThesisRevision
from money_pit.schemas.theses import ThesisStatus


_AS_OF = datetime(2026, 8, 1, tzinfo=UTC)


def _observation() -> ClaimObservation:
    return ClaimObservation(
        observation_id="observation-event",
        claim_text="A one-day promotion lifted unit sales.",
        claim_kind=ClaimKind.FACTUAL,
        category=ClaimCategory.CATALYST,
        source_item_id="source:item",
        evidence_fragment_ids=("fragment-1",),
        asserted_at=_AS_OF,
        known_at=_AS_OF,
        effective_from=_AS_OF,
        valid_until=_AS_OF + timedelta(days=2),
        horizon_class=HorizonClass.EVENT,
        instruments=("NEW",),
        causal_mechanisms=("promotion",),
        regime_assumptions=("normal demand",),
    )


def _revision() -> ThesisRevision:
    return ThesisRevision(
        revision_id="revision-1",
        thesis_id="thesis-1",
        revision_number=1,
        promoted_from_candidate_id="candidate-1",
        subject="Long-run distribution advantage",
        instrument="NEW",
        direction=ThesisDirection.LONG,
        status=ThesisStatus.ACTIVE,
        horizon_class=HorizonClass.STRUCTURAL,
        effective_from=_AS_OF,
        review_at=_AS_OF + timedelta(days=90),
        valid_until=_AS_OF + timedelta(days=400),
        scenario_distribution=(ScenarioOutcome(name="base", probability=1.0, expected_return=0.1),),
        invalidation_rules=("Distribution contracts",),
        supporting_claim_keys=("claim-event",),
        causal_mechanisms=("promotion",),
        regime_assumptions=("normal demand",),
        confidence=0.7,
        reasoning="The candidate requires a causal bridge from a short event to a structural thesis.",
        created_at=_AS_OF,
        known_at=_AS_OF,
    )


def test_temporal_compatibility_keeps_nonadjacent_horizons_separate_without_a_bridge() -> None:
    result = temporal_compatibility(
        _observation(),
        _revision(),
        as_of=_AS_OF,
        bridge_authorized=False,
        explicit_update=False,
    )

    assert not result.compatible


def test_temporal_compatibility_allows_an_explicit_causal_bridge() -> None:
    result = temporal_compatibility(
        _observation(),
        _revision(),
        as_of=_AS_OF,
        bridge_authorized=True,
        explicit_update=False,
    )

    assert result.compatible
