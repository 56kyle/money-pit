"""Tests for money_pit.pipeline.aggregator — the node merging source SignalSets into AggregatedSignals.

Backfills unit coverage for the newly-extracted helpers:
- _load_signal_sets raises FileNotFoundError when signals/ holds no *.json, else parses each SignalSet;
- _corroborate_claims splits the agent's agree/disagree relations into AGREE/DISAGREE CorroborationEntry
  lists, and applies tier_max re-tiering only when relations exist (claims pass through untouched otherwise);
- make_aggregator_node runs end-to-end over a working dir, writing aggregated_signals.json and threading
  run_has_actionable_content from compute_run_actionable.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from money_pit.compute.aggregation import compute_run_actionable
from money_pit.pipeline.aggregator import _corroborate_claims, _load_signal_sets, make_aggregator_node
from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.enums import ClaimCategory, ClaimRelationType, SignalTier, SourceType
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signals import AggregatedSignals, Claim, SignalSet

_SLUG = "test-run"
_SIGNALS_DIRNAME = "signals"


def _make_source_ref(source_id: str) -> SourceRef:
    return SourceRef(
        source_id=source_id,
        source_type=SourceType.MANUAL_NOTE,
        title="Test Source",
        url=None,
        published_at=None,
        retrieved_at="2026-01-01T00:00:00Z",
        locator=None,
    )


def _make_claim(claim_id: str, tier: SignalTier, *, source_id: str = "test-src") -> Claim:
    return Claim(
        claim_id=claim_id,
        tier=tier,
        claim="Test claim.",
        category=ClaimCategory.FUNDAMENTAL,
        tickers_affected=[],
        requires_validation=tier in (SignalTier.HIGH, SignalTier.MEDIUM),
        source_ref=_make_source_ref(source_id),
        cited_sources=[],
    )


def _make_signal_set(source_id: str, claims: list[Claim], *, has_actionable_content: bool) -> SignalSet:
    return SignalSet(
        slug=_SLUG,
        source_ref=_make_source_ref(source_id),
        summary="Test summary.",
        claims=claims,
        tickers_mentioned=[],
        sectors_mentioned=[],
        macro_themes=[],
        has_actionable_content=has_actionable_content,
    )


def _stub_agent(relations: ClaimRelations) -> Callable[[list[Claim]], ClaimRelations]:
    def _agent(_claims: list[Claim]) -> ClaimRelations:
        return relations

    return _agent


def _write_signal_sets(working_dir: Path, signal_sets: list[SignalSet]) -> None:
    signals_dir = working_dir / _SIGNALS_DIRNAME
    signals_dir.mkdir(parents=True, exist_ok=True)
    for signal_set in signal_sets:
        path = signals_dir / f"{signal_set.source_ref.source_id}.json"
        _ = path.write_text(signal_set.model_dump_json(indent=2), encoding="utf-8")


def test__load_signal_sets_with_absent_signals_dir_raises() -> None:
    with pytest.raises(FileNotFoundError):
        _ = _load_signal_sets(Path("/nonexistent-working-dir"))


def test__load_signal_sets_with_empty_signals_dir_raises(tmp_path: Path) -> None:
    (tmp_path / _SIGNALS_DIRNAME).mkdir()
    with pytest.raises(FileNotFoundError):
        _ = _load_signal_sets(tmp_path)


def test__load_signal_sets_with_signal_files_returns_all(tmp_path: Path) -> None:
    signal_sets = [
        _make_signal_set("src-a", [_make_claim("c-1", SignalTier.HIGH)], has_actionable_content=True),
        _make_signal_set("src-b", [_make_claim("c-2", SignalTier.LOW)], has_actionable_content=False),
    ]
    _write_signal_sets(tmp_path, signal_sets)
    result = _load_signal_sets(tmp_path)
    assert [s.source_ref.source_id for s in result] == ["src-a", "src-b"]


def test__corroborate_claims_with_agree_and_disagree_splits_entries() -> None:
    claims = [
        _make_claim("c-1", SignalTier.HIGH),
        _make_claim("c-2", SignalTier.HIGH),
        _make_claim("c-3", SignalTier.LOW),
    ]
    agent = _stub_agent(ClaimRelations(agree=[["c-1", "c-2"]], disagree=[["c-3"]]))

    _, corroboration_entries, conflict_entries = _corroborate_claims(agent, claims)

    assert [(e.relation, e.claim_ids) for e in corroboration_entries] == [
        (ClaimRelationType.AGREE, ["c-1", "c-2"])
    ]
    assert [(e.relation, e.claim_ids) for e in conflict_entries] == [(ClaimRelationType.DISAGREE, ["c-3"])]


def test__corroborate_claims_with_empty_relations_returns_claims_unchanged() -> None:
    claims = [_make_claim("c-1", SignalTier.LOW)]
    agent = _stub_agent(ClaimRelations(agree=[], disagree=[]))

    returned_claims, corroboration_entries, conflict_entries = _corroborate_claims(agent, claims)

    assert returned_claims == claims
    assert corroboration_entries == []
    assert conflict_entries == []


def test__corroborate_claims_with_relations_applies_tier_max() -> None:
    claims = [
        _make_claim("c-high", SignalTier.HIGH),
        _make_claim("c-low", SignalTier.LOW),
    ]
    agent = _stub_agent(ClaimRelations(agree=[["c-high", "c-low"]], disagree=[]))

    retiered_claims, _, _ = _corroborate_claims(agent, claims)

    by_id = {c.claim_id: c for c in retiered_claims}
    assert by_id["c-low"].tier == SignalTier.HIGH


def test_make_aggregator_node_with_signal_sets_writes_aggregated_json(tmp_path: Path) -> None:
    signal_sets = [
        _make_signal_set("src-a", [_make_claim("c-1", SignalTier.HIGH, source_id="src-a")], has_actionable_content=True),
        _make_signal_set("src-b", [_make_claim("c-2", SignalTier.LOW, source_id="src-b")], has_actionable_content=False),
    ]
    _write_signal_sets(tmp_path, signal_sets)
    node = make_aggregator_node(_stub_agent(ClaimRelations(agree=[], disagree=[])))

    _ = node({"slug": _SLUG, "working_dir": str(tmp_path)})

    aggregated = AggregatedSignals.model_validate_json(
        (tmp_path / "aggregated_signals.json").read_text(encoding="utf-8")
    )
    assert [c.claim_id for c in aggregated.claims] == ["c-1", "c-2"]
    assert [s.source_id for s in aggregated.sources] == ["src-a", "src-b"]


def test_make_aggregator_node_with_actionable_source_threads_actionable_flag(tmp_path: Path) -> None:
    signal_sets = [
        _make_signal_set("src-a", [_make_claim("c-1", SignalTier.HIGH)], has_actionable_content=True),
        _make_signal_set("src-b", [_make_claim("c-2", SignalTier.LOW)], has_actionable_content=False),
    ]
    _write_signal_sets(tmp_path, signal_sets)
    node = make_aggregator_node(_stub_agent(ClaimRelations(agree=[], disagree=[])))

    result = node({"slug": _SLUG, "working_dir": str(tmp_path)})

    assert result["run_has_actionable_content"] == compute_run_actionable(signal_sets)


def test_make_aggregator_node_with_no_actionable_source_flags_false(tmp_path: Path) -> None:
    signal_sets = [
        _make_signal_set("src-a", [_make_claim("c-1", SignalTier.LOW)], has_actionable_content=False),
    ]
    _write_signal_sets(tmp_path, signal_sets)
    node = make_aggregator_node(_stub_agent(ClaimRelations(agree=[], disagree=[])))

    result = node({"slug": _SLUG, "working_dir": str(tmp_path)})

    assert result["run_has_actionable_content"] is False
