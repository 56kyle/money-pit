"""A4 node: calls agents/thesis_judgment + compute/ post-processor functions, writes action_steps.json."""
import json
from pathlib import Path
from typing import Optional

from loguru import logger

from money_pit.compute.execution_params import build_execution_params
from money_pit.compute.regime import classify_regime
from money_pit.compute.sizing import size_position
from money_pit.config import Config
from money_pit.constants import ACTION_STEPS_JSON_FILENAME
from money_pit.constants import ACTION_STEPS_MD_FILENAME
from money_pit.constants import AGGREGATED_SIGNALS_JSON_FILENAME
from money_pit.constants import ANALYSIS_JUDGMENT_JSON_FILENAME
from money_pit.constants import ANALYSIS_MD_FILENAME
from money_pit.constants import INITIAL_ANSWERS_JSON_FILENAME
from money_pit.constants import PORTFOLIO_SNAPSHOT_FILENAME
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.pipeline._types import ThesisAgent
from money_pit.schemas import ExecutionParameters
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.analysis_draft import AnalysisHalt
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.analysis_draft import ScenarioTable
from money_pit.schemas.answers import Answer
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import RegimeTag
from money_pit.schemas.enums import Step1Disposition
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.macro import MacroIndicators
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.questions import INDICATOR_PREFIX
from money_pit.schemas.signals import AggregatedSignals


_PROBABILITY_PCT_TO_FRACTION: float = 100.0
_AGENT_EXCEPTION_HALT_REASON: str = "A4 thesis judgment agent raised an exception."


def _extract_macro_indicators(answers: list[Answer]) -> MacroIndicators:
    """Assemble MacroIndicators from macro_regime answers in initial_answers.json."""
    values: dict[str, float | None] = {
        "yield_curve": None,
        "credit_spreads": None,
        "pmi": None,
        "earnings_revisions": None,
        "inflation": None,
    }
    for ans in answers:
        if ans.category != QuestionCategory.MACRO_REGIME:
            continue
        if not ans.signal_source.startswith(INDICATOR_PREFIX):
            continue
        name = ans.signal_source[len(INDICATOR_PREFIX):]
        if name not in values:
            continue
        data = ans.data_retrieved
        if data is not None:
            raw = data.get("value")
            if isinstance(raw, (int, float)):
                values[name] = float(raw)
    return MacroIndicators(
        yield_curve=values["yield_curve"],
        credit_spreads=values["credit_spreads"],
        pmi=values["pmi"],
        earnings_revisions=values["earnings_revisions"],
        inflation=values["inflation"],
        as_of=None,
    )


def _to_scenario_list(table: ScenarioTable) -> list[tuple[float, float]]:
    """Convert a ScenarioTable to the (probability, return_pct) pairs consumed by compute functions."""
    return [
        (table.bull.probability / _PROBABILITY_PCT_TO_FRACTION, table.bull.return_pct),
        (table.base.probability / _PROBABILITY_PCT_TO_FRACTION, table.base.return_pct),
        (table.bear.probability / _PROBABILITY_PCT_TO_FRACTION, table.bear.return_pct),
    ]


def _favorable_label(favorable: bool | None) -> str:
    """Render a MacroIndicatorReading.favorable tri-state as a human-readable label."""
    if favorable is None:
        return "unknown"
    return "favorable" if favorable else "unfavorable"


def _render_action_steps_md(slug: str, action_steps: list[ActionStep]) -> str:
    """Render action_steps.md content from a list of ActionStep objects."""
    lines: list[str] = [f"# Action Steps — {slug}", ""]
    for step in action_steps:
        notional = step.execution_parameters.notional
        notional_str = f"${notional:.2f}" if notional is not None else "N/A"
        lines.extend([
            f"## {step.step_id} — {step.action_type.value} {step.instrument}",
            f"**{step.description}**",
            f"Regime: {step.regime_tag.value} | EV: {step.expected_value:.1%} | Conviction: {step.conviction.value}",
            f"Dollar amount: {notional_str}",
            f"Thesis: {step.one_sentence_thesis}",
            "",
        ])
    return "\n".join(lines)


def _render_analysis_md(
    slug: str,
    container: AnalysisJudgment,
    regime_tag: RegimeTag,
    action_steps: list[ActionStep],
) -> str:
    """Render the full A4 reasoning deterministically from the container and materialized steps."""
    lines: list[str] = [f"# Analysis — {slug}", ""]

    if container.halt is not None:
        lines.extend(["## Halt", container.halt.reason, ""])

    lines.extend(["## Regime", f"Deterministic regime tag: {regime_tag.value}", ""])

    lines.append("## Macro Read")
    if container.macro_read:
        for reading in container.macro_read:
            lines.append(
                f"- **{reading.indicator}** ({_favorable_label(reading.favorable)}): {reading.reading}"
            )
    else:
        lines.append("- No macro indicators reported.")
    lines.append("")

    lines.append("## Dropped Claims")
    if container.dropped_claims:
        for dropped in container.dropped_claims:
            lines.append(f"- **{dropped.claim_id}**: {dropped.reason}")
    else:
        lines.append("- No claims dropped.")
    lines.append("")

    lines.append("## Surviving Theses")
    if container.theses:
        for thesis in container.theses:
            lines.extend([
                f"### {thesis.claim_id} — {thesis.action_type.value} {thesis.instrument} ({thesis.disposition.value})",
                f"**{thesis.description}**",
                f"Thesis: {thesis.one_sentence_thesis}",
                f"EV: {thesis.expected_value:.1%} | Conviction: {thesis.conviction.value}",
                f"Sizing rationale: {thesis.sizing_rationale}",
                "",
            ])
    else:
        lines.extend(["- No theses survived.", ""])

    lines.append("## Action Steps")
    if action_steps:
        for step in action_steps:
            notional = step.execution_parameters.notional
            notional_str = f"${notional:.2f}" if notional is not None else "N/A"
            lines.append(
                f"- {step.step_id}: {step.action_type.value} {step.instrument} ({notional_str})"
            )
    else:
        lines.append("- No action steps produced.")
    lines.append("")

    return "\n".join(lines)


def _write_action_steps(working_dir: Path, slug: str, action_steps: list[ActionStep]) -> None:
    """Write action_steps.json and its markdown companion for the given steps."""
    _ = (working_dir / ACTION_STEPS_JSON_FILENAME).write_text(
        json.dumps([step.model_dump(mode="json") for step in action_steps], indent=2),
        encoding="utf-8",
    )
    _ = (working_dir / ACTION_STEPS_MD_FILENAME).write_text(
        _render_action_steps_md(slug, action_steps),
        encoding="utf-8",
    )


def make_analysis_node(
    config: Config,
    thesis_agent: ThesisAgent,
) -> PipelineNode:
    """Return a LangGraph node that runs A4 judgment and the deterministic post-processor."""

    def analysis_node(state: PipelineState) -> PipelineState:
        working_dir_str = state.get("working_dir")
        if working_dir_str is None:
            raise ValueError("PipelineState missing required key 'working_dir'")
        slug: Optional[str] = state.get("slug")
        if slug is None:
            raise ValueError("PipelineState missing required key 'slug'")
        working_dir: Path = Path(working_dir_str)

        aggregated_signals: AggregatedSignals = AggregatedSignals.model_validate_json(
            (working_dir / AGGREGATED_SIGNALS_JSON_FILENAME).read_text(encoding="utf-8")
        )
        portfolio_snapshot: PortfolioSnapshot = PortfolioSnapshot.model_validate_json(
            (working_dir / PORTFOLIO_SNAPSHOT_FILENAME).read_text(encoding="utf-8")
        )
        initial_answers: InitialAnswers = InitialAnswers.model_validate_json(
            (working_dir / INITIAL_ANSWERS_JSON_FILENAME).read_text(encoding="utf-8")
        )

        try:
            container: AnalysisJudgment = thesis_agent(
                aggregated_signals, portfolio_snapshot, initial_answers
            )
        except Exception:
            logger.exception("A4 thesis judgment agent failed; halting analysis")
            container = AnalysisJudgment(
                theses=[],
                dropped_claims=[],
                macro_read=[],
                halt=AnalysisHalt(reason=_AGENT_EXCEPTION_HALT_REASON),
            )

        _ = (working_dir / ANALYSIS_JUDGMENT_JSON_FILENAME).write_text(
            json.dumps(container.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )

        macro_indicators: MacroIndicators = _extract_macro_indicators(initial_answers.answers)
        regime_tag: RegimeTag = classify_regime(macro_indicators, config)

        if container.halt is not None:
            action_steps: list[ActionStep] = []
            _write_action_steps(working_dir, slug, action_steps)
            _ = (working_dir / ANALYSIS_MD_FILENAME).write_text(
                _render_analysis_md(slug, container, regime_tag, action_steps),
                encoding="utf-8",
            )
            return {
                "terminal_state": TerminalState.ANALYSIS_HALT,
                "completed_steps": [*(state.get("completed_steps") or []), "analysis"],
            }

        regime_uncertain: bool = regime_tag == RegimeTag.UNCERTAIN

        action_steps = []
        for i, thesis in enumerate(container.theses):
            verified: bool = thesis.disposition == Step1Disposition.SUPPORTED
            scenarios: list[tuple[float, float]] = _to_scenario_list(thesis.scenario_table)
            sector_headroom: float = config.sector_cap * portfolio_snapshot.total_account_value
            cash_headroom: float = max(
                0.0,
                portfolio_snapshot.available_cash
                - config.cash_min * portfolio_snapshot.total_account_value,
            )
            overlap_headroom: float = config.overlap_limit * portfolio_snapshot.total_account_value

            dollar_amount: Optional[float] = size_position(
                scenarios,
                portfolio_snapshot.total_account_value,
                config,
                verified,
                regime_uncertain,
                sector_headroom,
                cash_headroom,
                overlap_headroom,
            )
            if dollar_amount is None:
                continue

            step_id: str = f"A{i + 1:03d}"
            execution_parameters: ExecutionParameters = build_execution_params(
                step_id, slug, thesis.instrument, thesis.action_type, dollar_amount
            )

            action_steps.append(
                ActionStep(
                    step_id=step_id,
                    instrument=thesis.instrument,
                    action_type=thesis.action_type,
                    description=thesis.description,
                    group_id=thesis.group_id,
                    execution_parameters=execution_parameters,
                    one_sentence_thesis=thesis.one_sentence_thesis,
                    regime_tag=regime_tag,
                    expected_value=thesis.expected_value,
                    scenario_table=thesis.scenario_table,
                    invalidation_conditions=thesis.invalidation_conditions,
                    sizing_rationale=thesis.sizing_rationale,
                    conviction=thesis.conviction,
                )
            )

        _write_action_steps(working_dir, slug, action_steps)
        _ = (working_dir / ANALYSIS_MD_FILENAME).write_text(
            _render_analysis_md(slug, container, regime_tag, action_steps),
            encoding="utf-8",
        )

        if not action_steps:
            return {
                "terminal_state": TerminalState.NO_ACTION,
                "completed_steps": [*(state.get("completed_steps") or []), "analysis"],
            }

        return {
            "completed_steps": [*(state.get("completed_steps") or []), "analysis"],
        }

    return analysis_node
