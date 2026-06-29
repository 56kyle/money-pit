"""Node factory that merges source SignalSets, corroborates claims, and writes AggregatedSignals."""
from pathlib import Path
from typing import Callable

from money_pit.compute.aggregation import compute_run_actionable, tier_max, union_claims
from money_pit.graph.state import PipelineState
from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.enums import ClaimRelationType
from money_pit.schemas.signals import AggregatedSignals, Claim, CorroborationEntry, SignalSet


def make_aggregator_node(
    corroboration_agent: Callable[[list[Claim]], ClaimRelations],
) -> Callable[[PipelineState], dict[str, object]]:
    """Return a LangGraph node that merges all source SignalSets into AggregatedSignals."""

    def aggregator_node(state: PipelineState) -> dict[str, object]:
        working_dir_raw: str | None = state.get("working_dir")
        if working_dir_raw is None:
            raise ValueError("PipelineState missing required key 'working_dir'")
        slug: str | None = state.get("slug")
        if slug is None:
            raise ValueError("PipelineState missing required key 'slug'")
        working_dir: Path = Path(working_dir_raw)
        signals_dir: Path = working_dir / "signals"

        signal_files: list[Path] = sorted(signals_dir.glob("*.json"))
        if not signal_files:
            raise FileNotFoundError(f"No signal files found in {signals_dir}")

        signal_sets: list[SignalSet] = [
            SignalSet.model_validate_json(path.read_text(encoding="utf-8"))
            for path in signal_files
        ]

        claims: list[Claim] = union_claims(signal_sets)
        has_actionable: bool = compute_run_actionable(signal_sets)

        relations: ClaimRelations = corroboration_agent(claims)

        corroboration_entries: list[CorroborationEntry] = [
            CorroborationEntry(relation=ClaimRelationType.AGREE, claim_ids=group)
            for group in relations.agree
        ]
        conflict_entries: list[CorroborationEntry] = [
            CorroborationEntry(relation=ClaimRelationType.DISAGREE, claim_ids=group)
            for group in relations.disagree
        ]

        all_relations: list[CorroborationEntry] = corroboration_entries + conflict_entries
        if all_relations:
            claims = tier_max(claims, all_relations)

        aggregated: AggregatedSignals = AggregatedSignals(
            slug=slug,
            sources=[s.source_ref for s in signal_sets],
            claims=claims,
            corroborations=corroboration_entries,
            conflicts=conflict_entries,
            has_actionable_content=has_actionable,
        )

        _ = (working_dir / "aggregated_signals.json").write_text(
            aggregated.model_dump_json(indent=2),
            encoding="utf-8",
        )

        return {
            "completed_steps": list(state.get("completed_steps") or []) + ["aggregator"],
            "run_has_actionable_content": has_actionable,
        }

    return aggregator_node
