import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.intelligence_work import DiscoveryUnitKind
from money_pit.storage.intelligence_work import DiscoveryUnitRecord
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import ResearchJobRecord
from money_pit.storage.intelligence_work import SynthesisUnitRecord
from money_pit.storage.semantic_intelligence import HypothesisReviewDecision
from money_pit.storage.semantic_intelligence import HypothesisReviewStatus
from money_pit.storage.semantic_intelligence import ResearchJobDisposition
from money_pit.storage.semantic_intelligence import ResearchJobTaskBinding
from money_pit.storage.semantic_intelligence import ResearchTaskExecutionStatus
from money_pit.storage.semantic_intelligence import ResearchTaskRole
from money_pit.storage.semantic_intelligence import SemanticIntelligenceRepository
from money_pit.storage.semantic_intelligence import SemanticTransitionError
from money_pit.storage.semantic_intelligence import SynthesisDisposition
from money_pit.storage.semantic_intelligence import SynthesisEligibility
from money_pit.storage.semantic_intelligence import SynthesisMaterialState


if TYPE_CHECKING:
    import sqlite3


_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
_FINGERPRINT = "a" * 64


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "semantic.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def repository(database: Database) -> SemanticIntelligenceRepository:
    return SemanticIntelligenceRepository(database)


def _candidate(
    candidate_id: str,
    *,
    subject: str = "GDX operating leverage",
    theme: str | None = "Gold miners",
    horizon: HorizonClass = HorizonClass.MEDIUM_TERM,
    source_claim_key: str = "claim:gold",
) -> CandidateThesis:
    return CandidateThesis(
        candidate_thesis_id=candidate_id,
        subject=subject,
        direction=ThesisDirection.LONG,
        instrument_reference="VanEck Gold Miners ETF",
        instrument="GDX",
        theme=theme,
        horizon_class=horizon,
        discovery_basis=DiscoveryBasis(source_claim_keys=(source_claim_key,)),
        causal_mechanisms=("Operating leverage",),
        regime_assumptions=("Stable funding",),
        created_at=_NOW,
        known_at=_NOW,
    )


def _seed_candidate(database: Database, candidate: CandidateThesis) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO candidate_theses
            (candidate_thesis_id, status, created_at, known_at, candidate_json)
            VALUES (?, 'open', ?, ?, ?)""",
            (
                candidate.candidate_thesis_id,
                candidate.created_at.isoformat(),
                candidate.known_at.isoformat(),
                candidate.model_dump_json(),
            ),
        )


def _seed_task(
    database: Database,
    *,
    task_id: str,
    candidate_id: str,
    semantic_query: str | None = None,
    material_claim_key: str | None = None,
) -> None:
    payload = json.dumps(
        {
            "candidate_thesis_id": candidate_id,
            "provider": "sec",
            "query": semantic_query or f"{candidate_id} {task_id} material premise",
            "purpose": "Verify the material premise.",
            "material_claim_keys": [material_claim_key or f"claim:{candidate_id}"],
            "maximum_results": 5,
        }
    )
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO planned_research_tasks
            (task_id, candidate_thesis_id, status, created_at, known_at, task_json)
            VALUES (?, ?, 'pending', ?, ?, ?)""",
            (task_id, candidate_id, _NOW.isoformat(), _NOW.isoformat(), payload),
        )


def _seed_candidate_origin(
    database: Database,
    *,
    candidate_id: str,
    unit_id: str,
    source_id: str,
) -> None:
    run_id = f"run:{unit_id}"
    batch_id = f"batch:{unit_id}"
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO runs
            (run_id, requested_as_of, started_at, known_at, through_stage,
             source_config_hash, intelligence_config_hash, manifest_json)
            VALUES (?, ?, ?, ?, 'A2', 'sources', 'intelligence', '{}')""",
            (run_id, _NOW.isoformat(), _NOW.isoformat(), _NOW.isoformat()),
        )
        _ = connection.execute(
            """INSERT INTO discovery_units
            (unit_id, unit_kind, subject_id, input_fingerprint, source_id,
             status, created_at, completed_at, unit_json)
            VALUES (?, 'source_bundle', ?, ?, ?, 'completed', ?, ?, '{}')""",
            (unit_id, unit_id, _FINGERPRINT, source_id, _NOW.isoformat(), _NOW.isoformat()),
        )
        _ = connection.execute(
            """INSERT INTO discovery_batches
            (batch_id, run_id, status, created_at, completed_at, result_fingerprint,
             output_candidate_ids_json, validated_output_json, batch_json)
            VALUES (?, ?, 'completed', ?, ?, ?, ?, 'null', '{}')""",
            (
                batch_id,
                run_id,
                _NOW.isoformat(),
                _NOW.isoformat(),
                _FINGERPRINT,
                f'["{candidate_id}"]',
            ),
        )
        _ = connection.execute(
            "INSERT INTO discovery_batch_units (batch_id, unit_id) VALUES (?, ?)",
            (batch_id, unit_id),
        )
        _ = connection.execute(
            """INSERT INTO candidate_discovery_origins
            (candidate_thesis_id, unit_id, batch_id) VALUES (?, ?, ?)""",
            (candidate_id, unit_id, batch_id),
        )


def _seed_origin_unit(database: Database, unit_id: str) -> str:
    IntelligenceWorkRepository(database).append_discovery_unit(
        DiscoveryUnitRecord(
            unit_id=unit_id,
            kind=DiscoveryUnitKind.SOURCE_BUNDLE,
            subject_id=unit_id,
            input_fingerprint=_FINGERPRINT,
            source_id="source-a",
            created_at=_NOW,
            payload={},
        ),
        (),
    )
    run_id = f"run:{unit_id}"
    batch_id = f"batch:{unit_id}"
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO runs
            (run_id, requested_as_of, started_at, known_at, through_stage,
             source_config_hash, intelligence_config_hash, manifest_json)
            VALUES (?, ?, ?, ?, 'A2', 'sources', 'intelligence', '{}')""",
            (run_id, _NOW.isoformat(), _NOW.isoformat(), _NOW.isoformat()),
        )
        _ = connection.execute(
            """INSERT INTO discovery_batches
            (batch_id, run_id, status, created_at, output_candidate_ids_json,
             validated_output_json, batch_json)
            VALUES (?, ?, 'active', ?, '[]', 'null', '{}')""",
            (batch_id, run_id, _NOW.isoformat()),
        )
        _ = connection.execute(
            "INSERT INTO discovery_batch_units (batch_id, unit_id) VALUES (?, ?)",
            (batch_id, unit_id),
        )
    return batch_id


def _seed_job(
    database: Database,
    *,
    job_id: str,
    candidate_id: str,
    premise_fingerprint: str,
    origin_unit_ids: tuple[str, ...] = (),
) -> None:
    IntelligenceWorkRepository(database).ensure_research_job(
        ResearchJobRecord(
            job_id=job_id,
            candidate_thesis_id=candidate_id,
            premise_fingerprint=premise_fingerprint,
            created_at=_NOW,
            payload={},
        ),
        origin_unit_ids=origin_unit_ids,
    )


def _reconcile(
    database: Database,
    repository: SemanticIntelligenceRepository,
    candidate: CandidateThesis,
):
    _seed_candidate(database, candidate)
    return repository.reconcile_candidate(candidate, recorded_at=_NOW)


def _seed_claim_run(database: Database, run_id: str) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO runs
            (run_id, requested_as_of, started_at, known_at, through_stage,
             source_config_hash, intelligence_config_hash, manifest_json)
            VALUES (?, ?, ?, ?, 'A4', 'sources', 'intelligence', '{}')""",
            (run_id, _NOW.isoformat(), _NOW.isoformat(), _NOW.isoformat()),
        )


def _seed_eligible_synthesis_unit(
    database: Database,
    repository: SemanticIntelligenceRepository,
    *,
    candidate_id: str,
    hypothesis_id: str,
) -> None:
    _seed_job(
        database,
        job_id="research-job:synthesis",
        candidate_id=candidate_id,
        premise_fingerprint="1" * 64,
    )
    IntelligenceWorkRepository(database).ensure_synthesis_unit(
        SynthesisUnitRecord(
            unit_id="synthesis-unit:eligible",
            research_job_id="research-job:synthesis",
            input_fingerprint="2" * 64,
            created_at=_NOW,
            payload={},
        )
    )
    _ = repository.ensure_synthesis_material_state(
        SynthesisMaterialState(
            material_state_id="synthesis-material:eligible",
            hypothesis_id=hypothesis_id,
            material_fingerprint="3" * 64,
            eligibility=SynthesisEligibility.ELIGIBLE,
            created_at=_NOW,
            assessment={"evidence_standard_satisfied": True},
            research_job_ids=("research-job:synthesis",),
        )
    )
    repository.bind_synthesis_unit(
        unit_id="synthesis-unit:eligible",
        material_state_id="synthesis-material:eligible",
        disposition=SynthesisDisposition.CURRENT,
        recorded_at=_NOW,
    )


def _seed_thesis_revision(database: Database, *, candidate_id: str, revision_id: str) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            "INSERT INTO theses (thesis_id, candidate_thesis_id, created_at) VALUES (?, ?, ?)",
            ("thesis:1", candidate_id, _NOW.isoformat()),
        )
        _ = connection.execute(
            """INSERT INTO thesis_revisions
            (revision_id, thesis_id, revision_number, status, created_at, known_at,
             review_at, valid_until, revision_json)
            VALUES (?, 'thesis:1', 1, 'candidate', ?, ?, ?, NULL, '{}')""",
            (revision_id, _NOW.isoformat(), _NOW.isoformat(), _NOW.isoformat()),
        )


def _seed_synthesis_output(database: Database, *, revision_id: str) -> None:
    _seed_claim_run(database, "run:synthesis-output")
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """UPDATE synthesis_units SET status = 'completed', claimed_run_id = ?,
            claimed_at = ?, completed_at = ? WHERE unit_id = 'synthesis-unit:eligible'""",
            ("run:synthesis-output", _NOW.isoformat(), _NOW.isoformat()),
        )
        _ = connection.execute(
            """INSERT INTO synthesis_outputs
            (output_id, unit_id, output_fingerprint, created_at, output_record_ids_json, output_json)
            VALUES ('synthesis-output:1', 'synthesis-unit:eligible', ?, ?, ?, '{}')""",
            ("6" * 64, _NOW.isoformat(), f'["thesis_revision:{revision_id}"]'),
        )


def test_reconcile_candidate_with_exact_wording_and_source_variants_auto_links(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    first = _reconcile(database, repository, _candidate("candidate:1"))
    second = _reconcile(
        database,
        repository,
        _candidate(
            "candidate:2",
            subject="Miner margins expand faster than gold",
            source_claim_key="claim:independent-source",
        ),
    )

    assert (
        first.membership.variant_id,
        first.membership.group_id,
        first.reviews,
    ) == (
        second.membership.variant_id,
        second.membership.group_id,
        (),
    )


def test_admit_candidate_semantics_rolls_back_candidate_and_identity_after_late_origin_failure(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:crash")

    with pytest.raises(SemanticTransitionError):
        _ = repository.admit_candidate_semantics(
            candidate,
            batch_id="missing-batch",
            origin_unit_ids=("missing-unit",),
            recorded_at=_NOW,
        )

    with database.read_only_transaction() as connection:
        durable = tuple(
            cast(
                "sqlite3.Row",
                connection.execute(
                    """SELECT
                    (SELECT count(*) FROM candidate_theses WHERE candidate_thesis_id = ?),
                    (SELECT count(*) FROM candidate_hypothesis_memberships WHERE candidate_thesis_id = ?),
                    (SELECT count(*) FROM hypothesis_variants),
                    (SELECT count(*) FROM canonical_hypothesis_groups)""",
                    (candidate.candidate_thesis_id, candidate.candidate_thesis_id),
                ).fetchone(),
            )
        )
    assert durable == (0, 0, 0, 0)


def test_admit_candidate_semantics_retries_exact_paid_output_and_appends_new_origin(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:retry")
    first_batch_id = _seed_origin_unit(database, "unit:first")
    second_batch_id = _seed_origin_unit(database, "unit:second")
    first = repository.admit_candidate_semantics(
        candidate,
        batch_id=first_batch_id,
        origin_unit_ids=("unit:first",),
        recorded_at=_NOW,
    )

    second = repository.admit_candidate_semantics(
        candidate,
        batch_id=second_batch_id,
        origin_unit_ids=("unit:second",),
        recorded_at=_NOW,
    )

    with database.read_only_transaction() as connection:
        counts = tuple(
            cast(
                "sqlite3.Row",
                connection.execute(
                    """SELECT
                    (SELECT count(*) FROM candidate_theses WHERE candidate_thesis_id = ?),
                    (SELECT count(*) FROM candidate_hypothesis_memberships WHERE candidate_thesis_id = ?),
                    (SELECT count(*) FROM candidate_discovery_origins WHERE candidate_thesis_id = ?)""",
                    (
                        candidate.candidate_thesis_id,
                        candidate.candidate_thesis_id,
                        candidate.candidate_thesis_id,
                    ),
                ).fetchone(),
            )
        )
    assert (first.membership, second.membership, counts) == (first.membership, first.membership, (1, 1, 2))


def test_resolve_review_with_same_merges_effective_membership_once(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    first = _reconcile(database, repository, _candidate("candidate:1"))
    reconciliation = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    )
    review = reconciliation.reviews[0]

    resolved = repository.resolve_review(
        review.review_id,
        decision=HypothesisReviewDecision.SAME,
        actor="operator",
        reason="The themes describe the same economic exposure.",
        resolved_at=_NOW,
    )
    effective = repository.hypothesis_id_for_candidate("candidate:1")
    with database.transaction() as connection:
        group_rows = cast(
            "list[sqlite3.Row]",
            connection.execute("SELECT group_id, status FROM canonical_hypothesis_groups ORDER BY group_id").fetchall(),
        )
        review_row = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT review.created_at, resolution.decision
                FROM hypothesis_reviews review
                JOIN hypothesis_review_resolutions resolution USING (review_id)
                WHERE review.review_id = ?""",
                (review.review_id,),
            ).fetchone(),
        )

    assert (
        resolved.status,
        effective,
        repository.hypothesis_id_for_candidate("candidate:2"),
        effective not in {first.membership.group_id, reconciliation.membership.group_id},
        tuple(sorted(cast("str", row[1]) for row in group_rows)),
        (cast("str", review_row[0]), cast("str", review_row[1])),
    ) == (
        HypothesisReviewStatus.SAME,
        effective,
        effective,
        True,
        ("current", "superseded", "superseded"),
        (_NOW.isoformat(), "same"),
    )


def test_resolve_review_rejects_conflicting_second_decision(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    _ = _reconcile(database, repository, _candidate("candidate:1"))
    review = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    ).reviews[0]
    _ = repository.resolve_review(
        review.review_id,
        decision=HypothesisReviewDecision.DISTINCT,
        actor="operator",
        reason="Different causal framing.",
        resolved_at=_NOW,
    )

    with pytest.raises(SemanticTransitionError):
        _ = repository.resolve_review(
            review.review_id,
            decision=HypothesisReviewDecision.SAME,
            actor="operator",
            reason="Changed mind.",
            resolved_at=_NOW,
        )


def test_resolve_review_with_horizon_difference_cannot_merge_hypotheses(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    _ = _reconcile(database, repository, _candidate("candidate:1"))
    review = _reconcile(
        database,
        repository,
        _candidate("candidate:2", horizon=HorizonClass.TACTICAL),
    ).reviews[0]

    with pytest.raises(SemanticTransitionError):
        _ = repository.resolve_review(
            review.review_id,
            decision=HypothesisReviewDecision.SAME,
            actor="operator",
            reason="Attempted horizon merge.",
            resolved_at=_NOW,
        )


def test_list_reviews_with_source_uses_exact_candidate_lineage(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    _ = _reconcile(database, repository, _candidate("candidate:1"))
    review = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    ).reviews[0]
    _seed_candidate_origin(
        database,
        candidate_id="candidate:1",
        unit_id="discovery-unit:1",
        source_id="source-a",
    )

    assert (
        tuple(item.review_id for item in repository.list_reviews("source-a")),
        repository.list_reviews("source-b"),
    ) == ((review.review_id,), ())


def test_semantic_status_counts_only_unresolved_reviews(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    _ = _reconcile(database, repository, _candidate("candidate:1", theme="Gold miners"))
    second = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    )
    review = second.reviews[0]

    pending = repository.semantic_status()
    _ = repository.resolve_review(
        review.review_id,
        decision=HypothesisReviewDecision.DISTINCT,
        actor="operator",
        reason="Different economic exposure.",
        resolved_at=_NOW,
    )
    resolved = repository.semantic_status()

    assert (pending.needs_review, resolved.needs_review) == (1, 0)


def test_resolve_review_with_same_is_transitive_across_three_proposals(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    _ = _reconcile(database, repository, _candidate("candidate:1", theme="Gold miners"))
    second = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    )
    third = _reconcile(
        database,
        repository,
        _candidate("candidate:3", theme="Gold producer equities"),
    )
    review_12 = next(item for item in second.reviews if "candidate:1" in item.subject_candidate_id)
    review_23 = next(
        item
        for item in third.reviews
        if {item.subject_candidate_id, item.comparison_candidate_id} == {"candidate:2", "candidate:3"}
    )
    for review in (review_12, review_23):
        _ = repository.resolve_review(
            review.review_id,
            decision=HypothesisReviewDecision.SAME,
            actor="operator",
            reason="Same economic hypothesis.",
            resolved_at=_NOW,
        )

    effective_ids = {
        repository.hypothesis_id_for_candidate(candidate_id)
        for candidate_id in ("candidate:1", "candidate:2", "candidate:3")
    }

    assert len(effective_ids) == 1


def test_resolve_review_with_same_has_order_independent_group_identity_and_append_only_provenance(
    tmp_path: Path,
) -> None:
    def resolve_in_order(path: Path, pairs: tuple[frozenset[str], ...]) -> tuple[object, ...]:
        database = Database(path)
        database.initialize()
        repository = SemanticIntelligenceRepository(database)
        for candidate_id, theme in (
            ("candidate:1", "Gold miners"),
            ("candidate:2", "Precious-metal equities"),
            ("candidate:3", "Gold producer equities"),
        ):
            _ = _reconcile(database, repository, _candidate(candidate_id, theme=theme))
        reviews = {
            frozenset((review.subject_candidate_id, review.comparison_candidate_id)): review
            for review in repository.list_reviews()
        }
        for index, pair in enumerate(pairs, start=1):
            _ = repository.resolve_review(
                reviews[pair].review_id,
                decision=HypothesisReviewDecision.SAME,
                actor="operator",
                reason="Same economic hypothesis.",
                resolved_at=_NOW.replace(minute=index),
            )
        group_id = repository.hypothesis_id_for_candidate("candidate:1")
        assert group_id is not None
        with database.read_only_transaction() as connection:
            variant_rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT variant_id FROM canonical_hypothesis_group_variants
                    WHERE group_id = ? ORDER BY variant_id""",
                    (group_id,),
                ).fetchall(),
            )
            variants = tuple(cast("str", row[0]) for row in variant_rows)
            provenance_rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT successor_group_id, predecessor_group_id, review_id
                    FROM canonical_hypothesis_group_supersessions ORDER BY successor_group_id, predecessor_group_id"""
                ).fetchall(),
            )
            provenance = tuple(
                (cast("str", row[0]), cast("str", row[1]), cast("str", row[2])) for row in provenance_rows
            )
            resolution_row = cast(
                "sqlite3.Row",
                connection.execute("SELECT count(*) FROM hypothesis_review_resolutions").fetchone(),
            )
            superseded_row = cast(
                "sqlite3.Row",
                connection.execute(
                    "SELECT count(*) FROM canonical_hypothesis_groups WHERE status = 'superseded'"
                ).fetchone(),
            )
            durable_resolution_count = cast("int", resolution_row[0])
            superseded_group_count = cast("int", superseded_row[0])
        return group_id, variants, provenance, durable_resolution_count, superseded_group_count

    pair_12 = frozenset(("candidate:1", "candidate:2"))
    pair_23 = frozenset(("candidate:2", "candidate:3"))
    left = resolve_in_order(tmp_path / "left.sqlite3", (pair_12, pair_23))
    right = resolve_in_order(tmp_path / "right.sqlite3", (pair_23, pair_12))

    assert (left[:2], right[:2], left[2] != right[2], left[3:], right[3:]) == (
        left[:2],
        left[:2],
        True,
        (2, 4),
        (2, 4),
    )


def test_resolve_review_with_same_rejects_crossing_distinct_groups(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    _ = _reconcile(database, repository, _candidate("candidate:1", theme="Gold miners"))
    second = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    )
    third = _reconcile(
        database,
        repository,
        _candidate("candidate:3", theme="Gold producer equities"),
    )
    review_12 = next(
        item
        for item in second.reviews
        if {item.subject_candidate_id, item.comparison_candidate_id} == {"candidate:1", "candidate:2"}
    )
    review_13 = next(
        item
        for item in third.reviews
        if {item.subject_candidate_id, item.comparison_candidate_id} == {"candidate:1", "candidate:3"}
    )
    review_23 = next(
        item
        for item in third.reviews
        if {item.subject_candidate_id, item.comparison_candidate_id} == {"candidate:2", "candidate:3"}
    )
    _ = repository.resolve_review(
        review_12.review_id,
        decision=HypothesisReviewDecision.DISTINCT,
        actor="operator",
        reason="Economically distinct.",
        resolved_at=_NOW,
    )
    _ = repository.resolve_review(
        review_13.review_id,
        decision=HypothesisReviewDecision.SAME,
        actor="operator",
        reason="Same economic hypothesis.",
        resolved_at=_NOW,
    )

    with pytest.raises(SemanticTransitionError):
        _ = repository.resolve_review(
            review_23.review_id,
            decision=HypothesisReviewDecision.SAME,
            actor="operator",
            reason="Attempted crossing merge.",
            resolved_at=_NOW,
        )


def test_ensure_research_job_semantics_with_zero_one_three_tasks_preserves_one_head(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:1")
    hypothesis_id = _reconcile(database, repository, candidate).membership.group_id
    for index in range(3):
        _seed_task(database, task_id=f"task:{index}", candidate_id=candidate.candidate_thesis_id)
    for index in range(3):
        _seed_job(
            database,
            job_id=f"research-job:{index}",
            candidate_id=candidate.candidate_thesis_id,
            premise_fingerprint=str(index + 1) * 64,
        )
    _ = repository.ensure_research_job_semantics(
        job_id="research-job:0",
        hypothesis_id=hypothesis_id,
        scope_fingerprint=_FINGERPRINT,
        semantic_premise_fingerprint="1" * 64,
        task_bindings=(),
        recorded_at=_NOW,
    )
    _ = repository.ensure_research_job_semantics(
        job_id="research-job:1",
        hypothesis_id=hypothesis_id,
        scope_fingerprint=_FINGERPRINT,
        semantic_premise_fingerprint="2" * 64,
        task_bindings=(ResearchJobTaskBinding(task_id="task:0", role=ResearchTaskRole.INITIAL),),
        recorded_at=_NOW,
    )
    head = repository.ensure_research_job_semantics(
        job_id="research-job:2",
        hypothesis_id=hypothesis_id,
        scope_fingerprint=_FINGERPRINT,
        semantic_premise_fingerprint="3" * 64,
        task_bindings=tuple(
            ResearchJobTaskBinding(task_id=f"task:{index}", role=ResearchTaskRole.INITIAL) for index in range(3)
        ),
        recorded_at=_NOW,
    )

    assert (head.predecessor_job_ids, repository.runnable_research_job_ids()) == (
        ("research-job:1",),
        ("research-job:2",),
    )


def test_bind_research_job_tasks_adds_planner_followup_without_changing_premise(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:1")
    hypothesis_id = _reconcile(database, repository, candidate).membership.group_id
    for task_id in ("task:initial", "task:followup"):
        _seed_task(database, task_id=task_id, candidate_id=candidate.candidate_thesis_id)
    _seed_job(
        database,
        job_id="research-job:1",
        candidate_id=candidate.candidate_thesis_id,
        premise_fingerprint="1" * 64,
    )
    semantic = repository.ensure_research_job_semantics(
        job_id="research-job:1",
        hypothesis_id=hypothesis_id,
        scope_fingerprint=_FINGERPRINT,
        semantic_premise_fingerprint="2" * 64,
        task_bindings=(ResearchJobTaskBinding(task_id="task:initial", role=ResearchTaskRole.INITIAL),),
        recorded_at=_NOW,
    )

    repository.bind_research_job_tasks(
        job_id="research-job:1",
        bindings=(
            ResearchJobTaskBinding(
                task_id="task:followup",
                role=ResearchTaskRole.PLANNER_FOLLOWUP,
            ),
        ),
    )
    bindings = repository.task_bindings_for_job("research-job:1")

    assert (semantic.semantic_premise_fingerprint, tuple(item.role for item in bindings)) == (
        "2" * 64,
        (ResearchTaskRole.INITIAL, ResearchTaskRole.PLANNER_FOLLOWUP),
    )


def test_runnable_research_job_ids_respects_exact_source_scope(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:1")
    hypothesis_id = _reconcile(database, repository, candidate).membership.group_id
    for suffix in ("a", "b"):
        unit_id = f"discovery-unit:source-{suffix}"
        _seed_candidate_origin(
            database,
            candidate_id=candidate.candidate_thesis_id,
            unit_id=unit_id,
            source_id=f"source-{suffix}",
        )
        scope = suffix * 64
        job_id = f"research-job:{suffix}"
        _seed_job(
            database,
            job_id=job_id,
            candidate_id=candidate.candidate_thesis_id,
            premise_fingerprint=scope,
            origin_unit_ids=(unit_id,),
        )
        _ = repository.ensure_research_job_semantics(
            job_id=job_id,
            hypothesis_id=hypothesis_id,
            scope_fingerprint=scope,
            semantic_premise_fingerprint=scope,
            task_bindings=(),
            recorded_at=_NOW,
        )

    assert repository.runnable_research_job_ids(source_id="source-a") == ("research-job:a",)


def test_runnable_research_job_ids_excludes_hypothesis_awaiting_review(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    first = _reconcile(database, repository, _candidate("candidate:1", theme="Gold miners"))
    _ = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    )
    _seed_job(
        database,
        job_id="research-job:blocked",
        candidate_id="candidate:1",
        premise_fingerprint="4" * 64,
    )
    _ = repository.ensure_research_job_semantics(
        job_id="research-job:blocked",
        hypothesis_id=first.membership.group_id,
        scope_fingerprint="5" * 64,
        semantic_premise_fingerprint="4" * 64,
        task_bindings=(),
        recorded_at=_NOW,
    )

    assert repository.runnable_research_job_ids() == ()


def test_ensure_research_job_semantics_with_incomparable_predecessors_unions_and_reuses(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:1")
    hypothesis_id = _reconcile(database, repository, candidate).membership.group_id
    for task_id in ("task:a", "task:b"):
        _seed_task(database, task_id=task_id, candidate_id=candidate.candidate_thesis_id)
    for index in range(3):
        _seed_job(
            database,
            job_id=f"research-job:{index}",
            candidate_id=candidate.candidate_thesis_id,
            premise_fingerprint=str(index + 1) * 64,
        )
    _ = repository.ensure_research_job_semantics(
        job_id="research-job:0",
        hypothesis_id=hypothesis_id,
        scope_fingerprint="b" * 64,
        semantic_premise_fingerprint="1" * 64,
        task_bindings=(
            ResearchJobTaskBinding(
                task_id="task:a",
                role=ResearchTaskRole.INITIAL,
                execution_status=ResearchTaskExecutionStatus.COMPLETED,
                completed_at=_NOW,
            ),
        ),
        recorded_at=_NOW,
    )
    first_case = repository.ensure_research_job_semantics(
        job_id="research-job:1",
        hypothesis_id=hypothesis_id,
        scope_fingerprint="c" * 64,
        semantic_premise_fingerprint="2" * 64,
        task_bindings=(ResearchJobTaskBinding(task_id="task:b", role=ResearchTaskRole.INITIAL),),
        recorded_at=_NOW,
    )
    union = repository.ensure_research_job_semantics(
        job_id="research-job:2",
        hypothesis_id=hypothesis_id,
        scope_fingerprint="b" * 64,
        semantic_premise_fingerprint="3" * 64,
        task_bindings=(),
        predecessor_job_ids=("research-job:0", "research-job:1"),
        recorded_at=_NOW,
    )
    with database.transaction() as connection:
        rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """SELECT task_id, execution_status, reused_from_job_id, reused_from_task_id
                FROM research_job_tasks WHERE job_id = 'research-job:2' ORDER BY task_id"""
            ).fetchall(),
        )
    task_states = tuple(
        (
            cast("str", row[0]),
            cast("str", row[1]),
            cast("str | None", row[2]),
            cast("str | None", row[3]),
        )
        for row in rows
    )

    assert (
        first_case.disposition,
        union.predecessor_job_ids,
        task_states,
    ) == (
        ResearchJobDisposition.CURRENT,
        ("research-job:0", "research-job:1"),
        (
            ("task:a", "reused", "research-job:0", "task:a"),
            ("task:b", "pending", None, None),
        ),
    )


def test_successor_reuses_cross_candidate_semantic_task_and_unions_exact_origins(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    _ = _reconcile(database, repository, _candidate("candidate:1", theme="Gold miners"))
    second = _reconcile(database, repository, _candidate("candidate:2", theme="Precious-metal equities"))
    review = next(
        item
        for item in second.reviews
        if {item.subject_candidate_id, item.comparison_candidate_id} == {"candidate:1", "candidate:2"}
    )
    _ = repository.resolve_review(
        review.review_id,
        decision=HypothesisReviewDecision.SAME,
        actor="operator",
        reason="Same economic hypothesis.",
        resolved_at=_NOW,
    )
    hypothesis_id = repository.hypothesis_id_for_candidate("candidate:1")
    assert hypothesis_id is not None
    for candidate_id, task_id, unit_id in (
        ("candidate:1", "task:left", "unit:left"),
        ("candidate:2", "task:right", "unit:right"),
    ):
        _seed_candidate_origin(
            database,
            candidate_id=candidate_id,
            unit_id=unit_id,
            source_id="source-a",
        )
        _seed_task(
            database,
            task_id=task_id,
            candidate_id=candidate_id,
            semantic_query="GDX material premise",
            material_claim_key="claim:shared",
        )
    for job_id, candidate_id, unit_id, task_id, scope in (
        ("research-job:left", "candidate:1", "unit:left", "task:left", "1" * 64),
        ("research-job:right", "candidate:2", "unit:right", "task:right", "2" * 64),
    ):
        _seed_job(
            database,
            job_id=job_id,
            candidate_id=candidate_id,
            premise_fingerprint=scope,
            origin_unit_ids=(unit_id,),
        )
        _ = repository.ensure_research_job_semantics(
            job_id=job_id,
            hypothesis_id=hypothesis_id,
            scope_fingerprint=scope,
            semantic_premise_fingerprint=scope,
            task_bindings=(
                ResearchJobTaskBinding(
                    task_id=task_id,
                    role=ResearchTaskRole.INITIAL,
                    execution_status=ResearchTaskExecutionStatus.COMPLETED,
                    origin_unit_ids=(unit_id,),
                    completed_at=_NOW,
                ),
            ),
            recorded_at=_NOW,
        )
    _seed_job(
        database,
        job_id="research-job:union",
        candidate_id="candidate:1",
        premise_fingerprint="3" * 64,
    )

    _ = repository.ensure_research_job_semantics(
        job_id="research-job:union",
        hypothesis_id=hypothesis_id,
        scope_fingerprint="3" * 64,
        semantic_premise_fingerprint="3" * 64,
        task_bindings=(),
        predecessor_job_ids=("research-job:left", "research-job:right"),
        recorded_at=_NOW,
    )

    bindings = repository.task_bindings_for_job("research-job:union")
    assert tuple(
        (
            binding.execution_status,
            binding.reused_from_job_id,
            binding.reused_from_task_id,
            binding.origin_unit_ids,
        )
        for binding in bindings
    ) == (
        (
            ResearchTaskExecutionStatus.REUSED,
            "research-job:left",
            "task:left",
            ("unit:left", "unit:right"),
        ),
    )


def test_ensure_synthesis_material_state_reuses_operational_origins(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:1")
    hypothesis_id = _reconcile(database, repository, candidate).membership.group_id
    for index in range(2):
        _seed_job(
            database,
            job_id=f"research-job:{index}",
            candidate_id=candidate.candidate_thesis_id,
            premise_fingerprint=str(index + 1) * 64,
        )
    state = SynthesisMaterialState(
        material_state_id="synthesis-material:1",
        hypothesis_id=hypothesis_id,
        material_fingerprint="d" * 64,
        eligibility=SynthesisEligibility.ELIGIBLE,
        created_at=_NOW,
        assessment={"evidence_standard_satisfied": True},
        research_job_ids=("research-job:0",),
    )
    _ = repository.ensure_synthesis_material_state(state)

    reused = repository.ensure_synthesis_material_state(
        state.model_copy(update={"research_job_ids": ("research-job:1",)})
    )

    assert reused.research_job_ids == ("research-job:0", "research-job:1")


def test_eligible_synthesis_unit_ids_excludes_insufficient_evidence(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:1")
    hypothesis_id = _reconcile(database, repository, candidate).membership.group_id
    _seed_job(
        database,
        job_id="research-job:0",
        candidate_id=candidate.candidate_thesis_id,
        premise_fingerprint="1" * 64,
    )
    IntelligenceWorkRepository(database).ensure_synthesis_unit(
        SynthesisUnitRecord(
            unit_id="synthesis-unit:1",
            research_job_id="research-job:0",
            input_fingerprint="e" * 64,
            created_at=_NOW,
            payload={},
        )
    )
    _ = repository.ensure_synthesis_material_state(
        SynthesisMaterialState(
            material_state_id="synthesis-material:1",
            hypothesis_id=hypothesis_id,
            material_fingerprint="f" * 64,
            eligibility=SynthesisEligibility.INSUFFICIENT_EVIDENCE,
            created_at=_NOW,
            assessment={"evidence_standard_satisfied": False},
            research_job_ids=("research-job:0",),
        )
    )
    repository.bind_synthesis_unit(
        unit_id="synthesis-unit:1",
        material_state_id="synthesis-material:1",
        disposition=SynthesisDisposition.CURRENT,
        recorded_at=_NOW,
    )

    assert repository.eligible_synthesis_unit_ids() == ()


def test_claim_synthesis_unit_returns_none_for_hypothesis_awaiting_review(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    first = _reconcile(database, repository, _candidate("candidate:1", theme="Gold miners"))
    _ = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    )
    _seed_eligible_synthesis_unit(
        database,
        repository,
        candidate_id="candidate:1",
        hypothesis_id=first.membership.group_id,
    )
    _seed_claim_run(database, "run:claim")

    claimed = IntelligenceWorkRepository(database).claim_synthesis_unit(
        run_id="run:claim",
        claimed_at=_NOW,
        reclaim_before=_NOW,
    )

    assert claimed is None


def test_claim_synthesis_unit_claims_eligible_resolved_distinct_hypothesis(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    first = _reconcile(database, repository, _candidate("candidate:1", theme="Gold miners"))
    review = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    ).reviews[0]
    _ = repository.resolve_review(
        review.review_id,
        decision=HypothesisReviewDecision.DISTINCT,
        actor="operator",
        reason="Different economic exposure.",
        resolved_at=_NOW,
    )
    _seed_eligible_synthesis_unit(
        database,
        repository,
        candidate_id="candidate:1",
        hypothesis_id=first.membership.group_id,
    )
    _seed_claim_run(database, "run:claim")

    claimed = IntelligenceWorkRepository(database).claim_synthesis_unit(
        run_id="run:claim",
        claimed_at=_NOW,
        reclaim_before=_NOW,
    )

    claimed_unit_id = None if claimed is None else claimed.unit_id
    assert claimed_unit_id == "synthesis-unit:eligible"


def test_runtime_research_successor_supersedes_prior_synthesis_and_closes_a5_eligibility(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    reconciliation = _reconcile(database, repository, _candidate("candidate:1"))
    hypothesis_id = reconciliation.membership.group_id
    _seed_eligible_synthesis_unit(
        database,
        repository,
        candidate_id="candidate:1",
        hypothesis_id=hypothesis_id,
    )
    _ = repository.ensure_research_job_semantics(
        job_id="research-job:synthesis",
        hypothesis_id=hypothesis_id,
        scope_fingerprint="4" * 64,
        semantic_premise_fingerprint="1" * 64,
        task_bindings=(),
        recorded_at=_NOW,
    )
    _seed_thesis_revision(database, candidate_id="candidate:1", revision_id="revision:1")
    _seed_synthesis_output(database, revision_id="revision:1")
    before = repository.portfolio_eligibility_for_revision("revision:1")
    _seed_job(
        database,
        job_id="research-job:successor",
        candidate_id="candidate:1",
        premise_fingerprint="2" * 64,
    )

    _ = repository.ensure_research_job_semantics(
        job_id="research-job:successor",
        hypothesis_id=hypothesis_id,
        scope_fingerprint="4" * 64,
        semantic_premise_fingerprint="2" * 64,
        task_bindings=(),
        recorded_at=_NOW,
    )

    after = repository.portfolio_eligibility_for_revision("revision:1")
    with database.read_only_transaction() as connection:
        lifecycle = tuple(
            cast(
                "sqlite3.Row",
                connection.execute(
                    """SELECT disposition, successor_unit_id, successor_research_job_id
                    FROM synthesis_unit_semantics WHERE unit_id = 'synthesis-unit:eligible'"""
                ).fetchone(),
            )
        )
    assert (before.synthesis_evidence_sufficient, lifecycle, after.synthesis_evidence_sufficient) == (
        True,
        ("superseded", None, "research-job:successor"),
        False,
    )


def test_source_scoped_synthesis_status_excludes_mixed_origin_material(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:1")
    hypothesis_id = _reconcile(database, repository, candidate).membership.group_id
    for source_id, unit_id in (("source-a", "unit:a"), ("source-b", "unit:b")):
        _seed_candidate_origin(
            database,
            candidate_id=candidate.candidate_thesis_id,
            unit_id=unit_id,
            source_id=source_id,
        )
    _seed_job(
        database,
        job_id="research-job:mixed",
        candidate_id=candidate.candidate_thesis_id,
        premise_fingerprint="7" * 64,
        origin_unit_ids=("unit:a", "unit:b"),
    )
    _ = repository.ensure_research_job_semantics(
        job_id="research-job:mixed",
        hypothesis_id=hypothesis_id,
        scope_fingerprint="7" * 64,
        semantic_premise_fingerprint="7" * 64,
        task_bindings=(),
        recorded_at=_NOW,
    )
    IntelligenceWorkRepository(database).ensure_synthesis_unit(
        SynthesisUnitRecord(
            unit_id="synthesis-unit:mixed",
            research_job_id="research-job:mixed",
            input_fingerprint="8" * 64,
            created_at=_NOW,
            payload={},
        )
    )
    _ = repository.ensure_synthesis_material_state(
        SynthesisMaterialState(
            material_state_id="synthesis-material:mixed",
            hypothesis_id=hypothesis_id,
            material_fingerprint="8" * 64,
            eligibility=SynthesisEligibility.INSUFFICIENT_EVIDENCE,
            created_at=_NOW,
            assessment={"evidence_standard_satisfied": False},
            research_job_ids=("research-job:mixed",),
        )
    )
    repository.bind_synthesis_unit(
        unit_id="synthesis-unit:mixed",
        material_state_id="synthesis-material:mixed",
        disposition=SynthesisDisposition.UNAVAILABLE,
        recorded_at=_NOW,
    )

    status = repository.semantic_status(source_id="source-a")

    assert (status.unavailable_synthesis_units, status.insufficient_evidence_assessments) == (0, 0)


def test_portfolio_eligibility_is_unavailable_while_hypothesis_review_is_pending(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    _ = _reconcile(database, repository, _candidate("candidate:1", theme="Gold miners"))
    _ = _reconcile(
        database,
        repository,
        _candidate("candidate:2", theme="Precious-metal equities"),
    )
    _seed_thesis_revision(database, candidate_id="candidate:1", revision_id="revision:1")

    eligibility = repository.portfolio_eligibility_for_revision("revision:1")

    assert eligibility.model_dump() == {
        "revision_id": "revision:1",
        "intelligence_available": False,
        "synthesis_evidence_sufficient": False,
    }


def test_portfolio_eligibility_requires_eligible_synthesis_output_for_revision(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    reconciliation = _reconcile(database, repository, _candidate("candidate:1"))
    _seed_thesis_revision(database, candidate_id="candidate:1", revision_id="revision:1")
    _seed_eligible_synthesis_unit(
        database,
        repository,
        candidate_id="candidate:1",
        hypothesis_id=reconciliation.membership.group_id,
    )
    _seed_synthesis_output(database, revision_id="revision:1")

    eligibility = repository.portfolio_eligibility_for_revision("revision:1")

    assert eligibility.model_dump() == {
        "revision_id": "revision:1",
        "intelligence_available": True,
        "synthesis_evidence_sufficient": True,
    }


def test_lineage_returns_semantic_ancestors_without_mutation(
    database: Database,
    repository: SemanticIntelligenceRepository,
) -> None:
    candidate = _candidate("candidate:1")
    reconciliation = _reconcile(database, repository, candidate)
    _seed_candidate_origin(
        database,
        candidate_id=candidate.candidate_thesis_id,
        unit_id="discovery-unit:1",
        source_id="source-a",
    )

    lineage = repository.lineage(candidate.candidate_thesis_id)
    with database.transaction() as connection:
        membership_row = cast(
            "sqlite3.Row",
            connection.execute("SELECT count(*) FROM candidate_hypothesis_memberships").fetchone(),
        )
        membership_count = cast("int", membership_row[0])

    assert (lineage.hypothesis_ids, lineage.origin_unit_ids, membership_count) == (
        (reconciliation.membership.group_id,),
        ("discovery-unit:1",),
        1,
    )
