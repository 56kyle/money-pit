"""A4 node: calls agents/thesis_judgment + compute/ post-processor functions, writes action_steps.json."""
import json
from pathlib import Path
from typing import Callable

from money_pit.compute.execution_params import build_execution_params
from money_pit.compute.regime import classify_regime
from money_pit.compute.sizing import size_position
from money_pit.config import Config
from money_pit.graph.state import PipelineState
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.analysis_draft import ScenarioTable
from money_pit.schemas.answers import Answer
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import RegimeTag
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.macro import MacroIndicators
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.signals import AggregatedSignals


_PROBABILITY_PCT_TO_FRACTION: float = 100.0


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
        if not ans.signal_source.startswith("indicator:"):
            continue
        name = ans.signal_source[len("indicator:"):]
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


def make_analysis_node(
    config: Config,
    thesis_agent: Callable[[AggregatedSignals, PortfolioSnapshot, InitialAnswers], list[AnalysisJudgment]],
) -> Callable[[PipelineState], dict[str, object]]:
    """Return a LangGraph node that runs A4 judgment and the deterministic post-processor."""

    def analysis_node(state: PipelineState) -> dict[str, object]:
        working_dir_str = state.get("working_dir")
        if working_dir_str is None:
            raise ValueError("PipelineState missing required key 'working_dir'")
        slug = state.get("slug")
        if slug is None:
            raise ValueError("PipelineState missing required key 'slug'")
        working_dir = Path(working_dir_str)

        aggregated_signals = AggregatedSignals.model_validate_json(
            (working_dir / "aggregated_signals.json").read_text(encoding="utf-8")
        )
        portfolio_snapshot = PortfolioSnapshot.model_validate_json(
            (working_dir / "portfolio_snapshot.json").read_text(encoding="utf-8")
        )
        initial_answers = InitialAnswers.model_validate_json(
            (working_dir / "initial_answers.json").read_text(encoding="utf-8")
        )

        try:
            judgments: list[AnalysisJudgment] = thesis_agent(
                aggregated_signals, portfolio_snapshot, initial_answers
            )
        except Exception:
            _ = (working_dir / "analysis_judgment.json").write_text("[]", encoding="utf-8")
            _ = (working_dir / "action_steps.json").write_text("[]", encoding="utf-8")
            _ = (working_dir / "action_steps.md").write_text("", encoding="utf-8")
            analysis_halt_result: dict[str, object] = {
                "terminal_state": TerminalState.ANALYSIS_HALT,
                "completed_steps": [*(state.get("completed_steps") or []), "analysis"],
            }
            return analysis_halt_result

        _ = (working_dir / "analysis_judgment.json").write_text(
            json.dumps([j.model_dump(mode="json") for j in judgments], indent=2),
            encoding="utf-8",
        )

        if any(j.step_failed is not None for j in judgments):
            _ = (working_dir / "action_steps.json").write_text(
                json.dumps([], indent=2),
                encoding="utf-8",
            )
            _ = (working_dir / "action_steps.md").write_text("", encoding="utf-8")
            result: dict[str, object] = {
                "terminal_state": TerminalState.ANALYSIS_HALT,
                "completed_steps": [*(state.get("completed_steps") or []), "analysis"],
            }
            return result

        macro_indicators = _extract_macro_indicators(initial_answers.answers)
        regime_tag = classify_regime(macro_indicators, config)
        regime_uncertain: bool = regime_tag == RegimeTag.UNCERTAIN

        action_steps: list[ActionStep] = []
        for i, judgment in enumerate(judgments):
            if (
                judgment.instrument is None
                or judgment.action_type is None
                or judgment.scenario_table is None
                or judgment.description is None
                or judgment.one_sentence_thesis is None
                or judgment.expected_value is None
                or judgment.conviction is None
                or judgment.sizing_rationale is None
            ):
                continue

            scenarios = _to_scenario_list(judgment.scenario_table)
            sector_headroom: float = config.sector_cap * portfolio_snapshot.total_account_value
            cash_headroom: float = max(
                0.0,
                portfolio_snapshot.available_cash
                - config.cash_min * portfolio_snapshot.total_account_value,
            )
            overlap_headroom: float = config.overlap_limit * portfolio_snapshot.total_account_value

            dollar_amount = size_position(
                scenarios,
                portfolio_snapshot.total_account_value,
                config,
                False,
                regime_uncertain,
                sector_headroom,
                cash_headroom,
                overlap_headroom,
            )
            if dollar_amount is None:
                continue

            step_id: str = f"A{i + 1:03d}"
            execution_parameters = build_execution_params(
                step_id, slug, judgment.instrument, judgment.action_type, dollar_amount
            )

            action_steps.append(
                ActionStep(
                    step_id=step_id,
                    instrument=judgment.instrument,
                    action_type=judgment.action_type,
                    description=judgment.description,
                    group_id=judgment.group_id,
                    execution_parameters=execution_parameters,
                    one_sentence_thesis=judgment.one_sentence_thesis,
                    regime_tag=regime_tag,
                    expected_value=judgment.expected_value,
                    scenario_table=judgment.scenario_table,
                    invalidation_conditions=judgment.invalidation_conditions,
                    sizing_rationale=judgment.sizing_rationale,
                    conviction=judgment.conviction,
                )
            )

        _ = (working_dir / "action_steps.json").write_text(
            json.dumps([step.model_dump(mode="json") for step in action_steps], indent=2),
            encoding="utf-8",
        )
        _ = (working_dir / "action_steps.md").write_text(
            _render_action_steps_md(slug, action_steps),
            encoding="utf-8",
        )

        if not action_steps:
            no_action_result: dict[str, object] = {
                "terminal_state": TerminalState.NO_ACTION,
                "completed_steps": [*(state.get("completed_steps") or []), "analysis"],
            }
            return no_action_result

        completed_result: dict[str, object] = {
            "completed_steps": [*(state.get("completed_steps") or []), "analysis"],
        }
        return completed_result

    return analysis_node
