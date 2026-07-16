"""Module containing the A4 node that calls agents/thesis_judgment plus the compute/ post-processor functions and writes action_steps.json for the money_pit package."""

import json
import math
from pathlib import Path

from loguru import logger

from money_pit.compute.execution_params import BUY_SIDES
from money_pit.compute.execution_params import MIN_NOTIONAL_DOLLARS
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
from money_pit.contracts import ResolveInstrumentFacts
from money_pit.contracts import ThesisAgent
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.schemas import ExecutionParameters
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.analysis_draft import AnalysisHalt
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.analysis_draft import ScenarioTable
from money_pit.schemas.analysis_draft import ThesisJudgment
from money_pit.schemas.answers import Answer
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import ConvictionLevel
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import RegimeTag
from money_pit.schemas.enums import Step1Disposition
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.instrument import InstrumentFacts
from money_pit.schemas.macro import MACRO_INDICATOR_SERIES
from money_pit.schemas.macro import MacroIndicators
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.portfolio import Position
from money_pit.schemas.questions import INDICATOR_PREFIX
from money_pit.schemas.signals import AggregatedSignals


_PROBABILITY_PCT_TO_FRACTION: float = 100.0
_AGENT_EXCEPTION_HALT_REASON: str = "A4 thesis judgment agent raised an exception."


def _extract_macro_indicators(answers: list[Answer]) -> MacroIndicators:
    """Assemble MacroIndicators from macro_regime answers in initial_answers.json."""
    values: dict[str, float | None] = dict.fromkeys(MACRO_INDICATOR_SERIES, None)
    for ans in answers:
        if ans.category != QuestionCategory.MACRO_REGIME:
            continue
        if ans.signal_source is None or not ans.signal_source.startswith(INDICATOR_PREFIX):
            continue
        name: str = ans.signal_source[len(INDICATOR_PREFIX) :]
        if name not in values:
            continue
        data: dict[str, object] | None = ans.data_retrieved
        if data is not None:
            raw: object = data.get("value")
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
        notional: str | None = step.execution_parameters.notional
        notional_str: str = f"${notional}" if notional is not None else "N/A"
        lines.extend(
            [
                f"## {step.step_id} — {step.action_type.value} {step.instrument}",
                f"**{step.description}**",
                f"Regime: {step.regime_tag.value} | EV: {step.expected_value:.1%} | Conviction: {step.conviction.value}",
                f"Dollar amount: {notional_str}",
                f"Thesis: {step.one_sentence_thesis}",
                "",
            ]
        )
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
            lines.append(f"- **{reading.indicator}** ({_favorable_label(reading.favorable)}): {reading.reading}")
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
            lines.extend(
                [
                    f"### {thesis.claim_id} — {thesis.action_type.value} {thesis.instrument} ({thesis.disposition.value})",
                    f"**{thesis.description}**",
                    f"Thesis: {thesis.one_sentence_thesis}",
                    f"EV: {thesis.expected_value:.1%} | Conviction: {thesis.conviction.value}",
                    f"Sizing rationale: {thesis.sizing_rationale}",
                    "",
                ]
            )
    else:
        lines.extend(["- No theses survived.", ""])

    lines.append("## Action Steps")
    if action_steps:
        for step in action_steps:
            notional: str | None = step.execution_parameters.notional
            notional_str: str = f"${notional}" if notional is not None else "N/A"
            lines.append(f"- {step.step_id}: {step.action_type.value} {step.instrument} ({notional_str})")
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


def _load_analysis_inputs(
    working_dir: Path,
) -> tuple[AggregatedSignals, PortfolioSnapshot, InitialAnswers]:
    aggregated_signals: AggregatedSignals = AggregatedSignals.model_validate_json(
        (working_dir / AGGREGATED_SIGNALS_JSON_FILENAME).read_text(encoding="utf-8")
    )
    portfolio_snapshot: PortfolioSnapshot = PortfolioSnapshot.model_validate_json(
        (working_dir / PORTFOLIO_SNAPSHOT_FILENAME).read_text(encoding="utf-8")
    )
    initial_answers: InitialAnswers = InitialAnswers.model_validate_json(
        (working_dir / INITIAL_ANSWERS_JSON_FILENAME).read_text(encoding="utf-8")
    )
    return aggregated_signals, portfolio_snapshot, initial_answers


def _run_and_persist_thesis_judgment(
    thesis_agent: ThesisAgent,
    aggregated_signals: AggregatedSignals,
    portfolio_snapshot: PortfolioSnapshot,
    initial_answers: InitialAnswers,
    working_dir: Path,
) -> AnalysisJudgment:
    """Run A4 judgment, falling back to a halt container on agent failure."""
    try:
        container: AnalysisJudgment = thesis_agent(aggregated_signals, portfolio_snapshot, initial_answers)
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
    return container


def _conviction_rank(conviction: ConvictionLevel) -> int:
    """Rank convictions so highest sizes first: HIGH before MEDIUM before LOW."""
    return {ConvictionLevel.HIGH: 0, ConvictionLevel.MEDIUM: 1, ConvictionLevel.LOW: 2}[conviction]


def _priority_ordered(theses: list[ThesisJudgment]) -> list[tuple[int, ThesisJudgment]]:
    """Order theses by conviction, then expected value, breaking ties by original index."""
    return sorted(
        enumerate(theses),
        key=lambda it: (_conviction_rank(it[1].conviction), -it[1].expected_value, it[0]),
    )


def _sector_key(sector: str) -> str:
    """Normalize a sector label for case-insensitive tallying."""
    return sector.casefold()


def _seed_sector_exposure(positions: list[Position]) -> dict[str, float]:
    """Sum held current value per normalized sector to seed the running sector tally."""
    exposure: dict[str, float] = {}
    for position in positions:
        key: str = _sector_key(position.sector)
        exposure[key] = exposure.get(key, 0.0) + position.current_value
    return exposure


def _held_correlated_value(instrument: str, facts: InstrumentFacts, snapshot: PortfolioSnapshot) -> float:
    """Sum held value duplicative with the candidate: a name it holds, or an ETF that holds it."""
    instrument_key: str = instrument.upper()
    candidate_holdings: set[str] = {holding.upper() for holding in facts.holdings}
    total: float = 0.0
    for position in snapshot.positions:
        position_key: str = position.ticker.upper()
        if position_key == instrument_key:
            continue
        candidate_holds_position: bool = position_key in candidate_holdings
        etf_holds_candidate: bool = instrument_key in {
            holding.upper() for holding in snapshot.etf_holdings.get(position.ticker, [])
        }
        if candidate_holds_position or etf_holds_candidate:
            total += position.current_value
    return total


def _in_run_correlated_dollars(
    instrument: str,
    facts: InstrumentFacts,
    candidate_facts: dict[str, InstrumentFacts],
    in_run_by_ticker: dict[str, float],
) -> float:
    """Sum this-run allocations to candidates directly ETF-correlated with the candidate."""
    instrument_key: str = instrument.upper()
    candidate_holdings: set[str] = {holding.upper() for holding in facts.holdings}
    total: float = 0.0
    for prior, prior_dollars in in_run_by_ticker.items():
        prior_key: str = prior.upper()
        if prior_key == instrument_key:
            continue
        prior_held_by_candidate: bool = prior_key in candidate_holdings
        candidate_held_by_prior: bool = instrument_key in {
            holding.upper() for holding in candidate_facts[prior].holdings
        }
        if prior_held_by_candidate or candidate_held_by_prior:
            total += prior_dollars
    return total


def _build_action_step(
    step_id: str,
    thesis: ThesisJudgment,
    regime_tag: RegimeTag,
    execution_parameters: ExecutionParameters,
) -> ActionStep:
    return ActionStep(
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


def _materialize_action_steps(
    container: AnalysisJudgment,
    config: Config,
    portfolio_snapshot: PortfolioSnapshot,
    regime_tag: RegimeTag,
    slug: str,
    candidate_facts: dict[str, InstrumentFacts],
) -> list[ActionStep]:
    """Size each surviving thesis into an ActionStep, dropping any the sizer declines.

    Exposure-increasing theses size in priority order against running per-sector, cash,
    and candidate-vs-holdings overlap tallies; exposure-reducing theses are never clamped.
    """
    tav: float = portfolio_snapshot.total_account_value
    regime_uncertain: bool = regime_tag == RegimeTag.UNCERTAIN
    sector_used: dict[str, float] = _seed_sector_exposure(portfolio_snapshot.positions)
    cash_available: float = max(0.0, portfolio_snapshot.available_cash - config.cash_min * tav)
    in_run_by_ticker: dict[str, float] = {}

    action_steps: list[ActionStep] = []
    for _original_index, thesis in _priority_ordered(container.theses):
        verified: bool = thesis.disposition == Step1Disposition.SUPPORTED
        scenarios: list[tuple[float, float]] = _to_scenario_list(thesis.scenario_table)
        exposure_increasing: bool = thesis.action_type in BUY_SIDES

        if exposure_increasing:
            facts: InstrumentFacts = candidate_facts[thesis.instrument]
            sector_key: str = _sector_key(facts.sector)
            sector_headroom: float = max(0.0, config.sector_cap * tav - sector_used.get(sector_key, 0.0))
            correlated_dollars: float = _held_correlated_value(
                thesis.instrument, facts, portfolio_snapshot
            ) + _in_run_correlated_dollars(thesis.instrument, facts, candidate_facts, in_run_by_ticker)
            overlap_headroom: float = max(0.0, config.overlap_limit * tav - correlated_dollars)
            cash_headroom: float = cash_available
        else:
            sector_key = ""
            sector_headroom = overlap_headroom = cash_headroom = math.inf

        dollar_amount: float | None = size_position(
            scenarios,
            tav,
            config,
            verified,
            regime_uncertain,
            sector_headroom,
            cash_headroom,
            overlap_headroom,
        )
        if dollar_amount is None or dollar_amount < MIN_NOTIONAL_DOLLARS:
            continue

        if exposure_increasing:
            sector_used[sector_key] = sector_used.get(sector_key, 0.0) + dollar_amount
            cash_available -= dollar_amount
            in_run_by_ticker[thesis.instrument] = in_run_by_ticker.get(thesis.instrument, 0.0) + dollar_amount

        step_id: str = f"A{len(action_steps) + 1:03d}"
        execution_parameters: ExecutionParameters = build_execution_params(
            step_id, slug, thesis.instrument, thesis.action_type, dollar_amount
        )
        action_steps.append(_build_action_step(step_id, thesis, regime_tag, execution_parameters))

    return action_steps


def _persist_analysis_outputs(
    working_dir: Path,
    slug: str,
    container: AnalysisJudgment,
    regime_tag: RegimeTag,
    action_steps: list[ActionStep],
) -> None:
    """Write action_steps.json/.md and analysis.md for both the halt and normal paths."""
    _write_action_steps(working_dir, slug, action_steps)
    _ = (working_dir / ANALYSIS_MD_FILENAME).write_text(
        _render_analysis_md(slug, container, regime_tag, action_steps),
        encoding="utf-8",
    )


def make_analysis_node(
    config: Config,
    thesis_agent: ThesisAgent,
    resolve_instrument_facts: ResolveInstrumentFacts,
) -> PipelineNode:
    """Return a LangGraph node that runs A4 judgment and the deterministic post-processor.

    The returned node fails closed on an unpinned, absent, or malformed order schema:
    AlpacaOrderSchemaNotPinnedError / AlpacaOrderSchemaMissingError /
    AlpacaOrderSchemaMalformedError (and jsonschema.ValidationError when the emitted
    payload violates the schema, and InvalidExecutionAmountError when the sized dollar
    amount is not finite or is below the minimum notional) propagate uncaught from
    build_execution_params. This is a deliberate asymmetry with the A4 agent failure
    path, which is caught and turned into an ANALYSIS_HALT.
    """

    def analysis_node(state: PipelineState) -> PipelineState:
        working_dir: Path = require_working_dir(state)
        slug: str = require_slug(state)

        aggregated_signals, portfolio_snapshot, initial_answers = _load_analysis_inputs(working_dir)
        container: AnalysisJudgment = _run_and_persist_thesis_judgment(
            thesis_agent, aggregated_signals, portfolio_snapshot, initial_answers, working_dir
        )

        macro_indicators: MacroIndicators = _extract_macro_indicators(initial_answers.answers)
        regime_tag: RegimeTag = classify_regime(macro_indicators, config)

        if container.halt is not None:
            _persist_analysis_outputs(working_dir, slug, container, regime_tag, [])
            return {
                "terminal_state": TerminalState.ANALYSIS_HALT,
                "completed_steps": with_completed_step(state, "analysis"),
            }

        candidate_instruments: list[str] = list(
            dict.fromkeys(
                thesis.instrument for thesis in container.theses if thesis.action_type in BUY_SIDES
            )
        )
        candidate_facts: dict[str, InstrumentFacts] = {
            instrument: resolve_instrument_facts(instrument) for instrument in candidate_instruments
        }

        action_steps: list[ActionStep] = _materialize_action_steps(
            container, config, portfolio_snapshot, regime_tag, slug, candidate_facts
        )
        _persist_analysis_outputs(working_dir, slug, container, regime_tag, action_steps)

        if not action_steps:
            return {
                "terminal_state": TerminalState.NO_ACTION,
                "completed_steps": with_completed_step(state, "analysis"),
            }

        return {
            "completed_steps": with_completed_step(state, "analysis"),
        }

    return analysis_node
