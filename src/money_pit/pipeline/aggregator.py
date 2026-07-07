"""Module containing the node factory that merges source SignalSets, corroborates claims, and writes AggregatedSignals for the money_pit package."""

from pathlib import Path

from money_pit.compute.aggregation import compute_run_actionable
from money_pit.compute.aggregation import tier_max
from money_pit.compute.aggregation import union_claims
from money_pit.constants import AGGREGATED_SIGNALS_JSON_FILENAME
from money_pit.constants import AGGREGATED_SIGNALS_MD_FILENAME
from money_pit.constants import SIGNALS_DIRNAME
from money_pit.contracts import CorroborationAgent
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.enums import ClaimRelationType
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.signals import Claim
from money_pit.schemas.signals import CorroborationEntry
from money_pit.schemas.signals import SignalSet


def _render_aggregated_md(aggregated: AggregatedSignals) -> str:
    """Render aggregated_signals.md deterministically from the aggregated signals."""
    actionable_label: str = "yes" if aggregated.has_actionable_content else "no"
    lines: list[str] = [
        f"# Aggregated Signals — {aggregated.slug}",
        "",
        f"Sources: {len(aggregated.sources)}",
        f"Actionable content: {actionable_label}",
        "",
        f"## Claims ({len(aggregated.claims)})",
    ]
    if aggregated.claims:
        for claim in aggregated.claims:
            tickers: str = ", ".join(claim.tickers_affected) or "none"
            lines.append(f"- **{claim.claim_id}** [{claim.tier.value}] ({tickers}): {claim.claim}")
    else:
        lines.append("- No claims.")

    lines.append("")
    lines.append(f"## Corroborations ({len(aggregated.corroborations)})")
    for entry in aggregated.corroborations:
        lines.append(f"- {', '.join(entry.claim_ids)}")

    lines.append("")
    lines.append(f"## Conflicts ({len(aggregated.conflicts)})")
    for entry in aggregated.conflicts:
        lines.append(f"- {', '.join(entry.claim_ids)}")

    return "\n".join(lines) + "\n"


def _load_signal_sets(working_dir: Path) -> list[SignalSet]:
    """Load and parse every source SignalSet from the working directory's signals folder."""
    signals_dir: Path = working_dir / SIGNALS_DIRNAME
    signal_files: list[Path] = sorted(signals_dir.glob("*.json"))
    if not signal_files:
        raise FileNotFoundError(f"No signal files found in {signals_dir}")
    return [SignalSet.model_validate_json(path.read_text(encoding="utf-8")) for path in signal_files]


def _corroborate_claims(
    corroboration_agent: CorroborationAgent,
    claims: list[Claim],
) -> tuple[list[Claim], list[CorroborationEntry], list[CorroborationEntry]]:
    """Corroborate claims, returning re-tiered claims plus the agree and disagree entries."""
    relations: ClaimRelations = corroboration_agent(claims)
    corroboration_entries: list[CorroborationEntry] = [
        CorroborationEntry(relation=ClaimRelationType.AGREE, claim_ids=group) for group in relations.agree
    ]
    conflict_entries: list[CorroborationEntry] = [
        CorroborationEntry(relation=ClaimRelationType.DISAGREE, claim_ids=group) for group in relations.disagree
    ]
    all_relations: list[CorroborationEntry] = corroboration_entries + conflict_entries
    if all_relations:
        claims = tier_max(claims, all_relations)
    return claims, corroboration_entries, conflict_entries


def _write_aggregated(working_dir: Path, aggregated: AggregatedSignals) -> None:
    """Write the aggregated signals to their JSON and Markdown artifacts."""
    json_path: Path = working_dir / AGGREGATED_SIGNALS_JSON_FILENAME
    json_path.write_text(aggregated.model_dump_json(indent=2), encoding="utf-8")

    md_path: Path = working_dir / AGGREGATED_SIGNALS_MD_FILENAME
    md_path.write_text(_render_aggregated_md(aggregated=aggregated), encoding="utf-8")


def make_aggregator_node(
    corroboration_agent: CorroborationAgent,
) -> PipelineNode:
    """Return a LangGraph node that merges all source SignalSets into AggregatedSignals."""

    def aggregator_node(state: PipelineState) -> PipelineState:
        working_dir: Path = require_working_dir(state)
        slug: str = require_slug(state)

        signal_sets: list[SignalSet] = _load_signal_sets(working_dir)
        claims: list[Claim] = union_claims(signal_sets)
        has_actionable: bool = compute_run_actionable(signal_sets)

        claims, corroboration_entries, conflict_entries = _corroborate_claims(corroboration_agent, claims)

        aggregated: AggregatedSignals = AggregatedSignals(
            slug=slug,
            sources=[s.source_ref for s in signal_sets],
            claims=claims,
            corroborations=corroboration_entries,
            conflicts=conflict_entries,
            has_actionable_content=has_actionable,
        )
        _write_aggregated(working_dir, aggregated)

        return {
            "completed_steps": with_completed_step(state, "aggregator"),
            "run_has_actionable_content": has_actionable,
        }

    return aggregator_node
