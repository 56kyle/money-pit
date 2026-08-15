from datetime import UTC
from datetime import datetime
from datetime import timedelta

from money_pit.contracts import ResearchTaskDraft
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.schemas.universe import UniverseLayer
from money_pit.semantic_identity import CapitalReferenceKind
from money_pit.semantic_identity import candidate_review_dimensions
from money_pit.semantic_identity import candidate_semantic_variant
from money_pit.semantic_identity import research_premise_semantics
from money_pit.semantic_identity import research_task_semantics


_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _candidate(
    *,
    candidate_id: str = "candidate-1",
    subject: str = "Gold miners benefit from operating leverage",
    horizon_class: HorizonClass = HorizonClass.MEDIUM_TERM,
    theme: str | None = "Gold Miner Operating Leverage",
    causal_mechanisms: tuple[str, ...] = ("Margin expansion", "Higher gold prices"),
    discovery_basis: DiscoveryBasis | None = None,
    instrument_reference: str | None = " gdx ",
    instrument: str | None = "GDX",
) -> CandidateThesis:
    return CandidateThesis(
        candidate_thesis_id=candidate_id,
        subject=subject,
        direction=ThesisDirection.LONG,
        instrument_reference=instrument_reference,
        instrument=instrument,
        theme=theme,
        horizon_class=horizon_class,
        discovery_basis=discovery_basis or DiscoveryBasis(source_claim_keys=(f"claim:{candidate_id}",)),
        causal_mechanisms=causal_mechanisms,
        regime_assumptions=("No severe funding stress",),
        created_at=_NOW,
        known_at=_NOW,
    )


def _claim(*, projected_as_of: datetime = _NOW, next_refresh_at: datetime | None = None) -> CanonicalClaim:
    return CanonicalClaim(
        canonical_claim_key="claim:gold-price",
        current_status=ClaimStatus.ACTIVE,
        active_observation_ids=("observation-1",),
        projected_as_of=projected_as_of,
        last_material_change_at=_NOW - timedelta(days=1),
        next_refresh_at=next_refresh_at,
        freshness_policy_version="freshness-v1",
    )


def _task(
    *,
    candidate_id: str = "candidate-1",
    candidate_subject: str = "Gold miners",
    query: str = " GDX   operating leverage ",
) -> ResearchTaskDraft:
    return ResearchTaskDraft(
        candidate_thesis_id=candidate_id,
        candidate_subject=candidate_subject,
        provider=" SEC ",
        query=query,
        purpose=" Verify   operating leverage ",
        material_claim_keys=("claim:b", "claim:a", "claim:a"),
        maximum_results=3,
    )


def test_candidate_semantic_variant_with_different_wording_and_provenance_is_exact() -> None:
    left = candidate_semantic_variant(_candidate())
    right = candidate_semantic_variant(
        _candidate(
            candidate_id="candidate-2",
            subject="GDX offers upside when miner margins widen",
            theme=" gold miner operating leverage ",
            causal_mechanisms=("higher   gold prices", "MARGIN EXPANSION"),
            discovery_basis=DiscoveryBasis(
                universe_layer=UniverseLayer.EXPLICIT_PROXY,
                universe_reference="GDX",
            ),
        )
    )

    assert left == right


def test_candidate_semantic_variant_prefers_resolved_instrument() -> None:
    variant = candidate_semantic_variant(_candidate(instrument="GDX", instrument_reference="VanEck Gold Miners ETF"))

    assert (variant.capital_kind, variant.capital_reference) == (
        CapitalReferenceKind.RESOLVED_INSTRUMENT,
        "GDX",
    )


def test_candidate_semantic_variant_uses_unresolved_instrument_reference() -> None:
    variant = candidate_semantic_variant(_candidate(instrument=None, instrument_reference=" VanEck   Gold Miners ETF "))

    assert (variant.capital_kind, variant.capital_reference) == (
        CapitalReferenceKind.INSTRUMENT_REFERENCE,
        "VANECK GOLD MINERS ETF",
    )


def test_candidate_semantic_variant_qualifies_universe_reference_by_layer() -> None:
    variant = candidate_semantic_variant(
        _candidate(
            instrument=None,
            instrument_reference=None,
            discovery_basis=DiscoveryBasis(
                universe_layer=UniverseLayer.EXPLICIT_PROXY,
                universe_reference=" gdx ",
            ),
        )
    )

    assert (variant.capital_kind, variant.capital_reference) == (
        CapitalReferenceKind.UNIVERSE_REFERENCE,
        "explicit_proxy:GDX",
    )


def test_candidate_semantic_variant_with_theme_only_never_auto_links() -> None:
    left = candidate_semantic_variant(
        _candidate(
            candidate_id="candidate-theme-1",
            instrument=None,
            instrument_reference=None,
            theme=" Gold miners ",
        )
    )
    right = candidate_semantic_variant(
        _candidate(
            candidate_id="candidate-theme-2",
            instrument=None,
            instrument_reference=None,
            theme="Gold miners",
        )
    )

    assert (left.capital_kind, left.variant_id != right.variant_id) == (
        CapitalReferenceKind.UNCLASSIFIED_CANDIDATE,
        True,
    )


def test_candidate_semantic_variant_with_source_mention_universe_reference_is_available() -> None:
    variant = candidate_semantic_variant(
        _candidate(
            instrument=None,
            instrument_reference=None,
            theme="Bonds",
            discovery_basis=DiscoveryBasis(
                universe_layer=UniverseLayer.SOURCE_MENTION,
                universe_reference=" BONDS ",
            ),
        )
    )

    assert (variant.capital_kind, variant.capital_reference) == (
        CapitalReferenceKind.UNIVERSE_REFERENCE,
        "source_mention:BONDS",
    )


def test_candidate_semantic_variant_with_unclassified_capital_never_auto_links() -> None:
    left = candidate_semantic_variant(
        _candidate(
            candidate_id="candidate-unclassified-1",
            instrument=None,
            instrument_reference=None,
            theme=None,
        )
    )
    right = candidate_semantic_variant(
        _candidate(
            candidate_id="candidate-unclassified-2",
            instrument=None,
            instrument_reference=None,
            theme=None,
        )
    )

    assert left.variant_id != right.variant_id


def test_candidate_semantic_variant_with_different_horizon_is_not_exact() -> None:
    medium_term = candidate_semantic_variant(_candidate())
    tactical = candidate_semantic_variant(_candidate(horizon_class=HorizonClass.TACTICAL))

    assert medium_term.variant_id != tactical.variant_id


def test_candidate_review_dimensions_with_different_horizon_requires_review() -> None:
    medium_term = candidate_semantic_variant(_candidate())
    tactical = candidate_semantic_variant(_candidate(horizon_class=HorizonClass.TACTICAL))

    assert candidate_review_dimensions(medium_term, tactical) == ("horizon_class",)


def test_candidate_review_dimensions_with_different_mechanism_requires_review() -> None:
    operating_leverage = candidate_semantic_variant(_candidate())
    reserve_growth = candidate_semantic_variant(_candidate(causal_mechanisms=("Reserve growth", "Higher gold prices")))

    assert candidate_review_dimensions(operating_leverage, reserve_growth) == ("causal_mechanisms",)


def test_research_task_semantics_excludes_candidate_aliases() -> None:
    left = research_task_semantics(_task())
    right = research_task_semantics(_task(candidate_id="candidate-2", candidate_subject="Reworded GDX proposal"))

    assert left == right


def test_research_premise_semantics_excludes_projection_clock_changes() -> None:
    hypothesis_id = "hypothesis:group-1"
    initial = research_premise_semantics(
        hypothesis_id=hypothesis_id,
        material_claims=(_claim(),),
        initial_tasks=(_task(),),
    )
    later_projection = research_premise_semantics(
        hypothesis_id=hypothesis_id,
        material_claims=(
            _claim(
                projected_as_of=_NOW + timedelta(days=2),
                next_refresh_at=_NOW + timedelta(days=7),
            ),
        ),
        initial_tasks=(_task(candidate_id="candidate-2"),),
    )

    assert initial.fingerprint == later_projection.fingerprint


def test_research_premise_semantics_changes_with_initial_task_set() -> None:
    hypothesis_id = "hypothesis:group-1"
    initial = research_premise_semantics(
        hypothesis_id=hypothesis_id,
        material_claims=(_claim(),),
        initial_tasks=(_task(),),
    )
    expanded = research_premise_semantics(
        hypothesis_id=hypothesis_id,
        material_claims=(_claim(),),
        initial_tasks=(_task(), _task(query="GDX realized margin sensitivity")),
    )

    assert initial.fingerprint != expanded.fingerprint
