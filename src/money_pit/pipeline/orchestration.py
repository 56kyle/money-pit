"""Scheduler trigger, working-dir creation, slug assignment."""
import shutil
from datetime import datetime
from datetime import timezone
from pathlib import Path

from money_pit.config import load_config
from money_pit.graph.graph import build_graph
from money_pit.graph.state import PipelineState
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.analysis_draft import Scenario
from money_pit.schemas.analysis_draft import ScenarioTable
from money_pit.schemas.answers import Answer
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import ActionType
from money_pit.schemas.enums import Confidence
from money_pit.schemas.enums import ConvictionLevel
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.questions import Question
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.signals import Claim


def _stub_fetch_portfolio(slug: str) -> PortfolioSnapshot:
    """Return a minimal portfolio snapshot for Phase 4 testing."""
    return PortfolioSnapshot(
        slug=slug,
        as_of=slug,
        total_account_value=100_000.0,
        available_cash=20_000.0,
        positions=[],
        sector_weights={},
        correlated_overlaps=[],
    )


def _stub_corroboration(_claims: list[Claim]) -> ClaimRelations:
    """Return empty agree/disagree relations — no LLM corroboration in Phase 4."""
    return ClaimRelations(agree=[], disagree=[])


def _stub_claim_questions(_claims: list[Claim]) -> list[Question]:
    """Return no LLM-generated questions in Phase 4."""
    return []


def _stub_answer_synthesis(
    questions: list[Question],
    _sources: list[SourceRef],
) -> list[Answer]:
    """Return one stub answer per question."""
    return [
        Answer(
            question_id=q.id,
            question=q.question,
            category=q.category,
            signal_source=q.signal_source,
            signal_tier=q.signal_tier,
            answer="Stub answer — Phase 4 testing only.",
            confidence=Confidence.LOW,
            sources_used=[],
            data_retrieved=None,
            limitations="Phase 4 stub; no real retrieval performed.",
        )
        for q in questions
    ]


def _stub_thesis_agent(
    aggregated_signals: AggregatedSignals,
    _portfolio_snapshot: PortfolioSnapshot,
    _initial_answers: InitialAnswers,
) -> list[AnalysisJudgment]:
    """Return one stub judgment for the first high/medium-tier claim with a ticker, or [] if none."""
    qualifying_claim: Claim | None = next(
        (
            c
            for c in aggregated_signals.claims
            if c.tier in (SignalTier.HIGH, SignalTier.MEDIUM) and c.tickers_affected
        ),
        None,
    )
    if qualifying_claim is None:
        return []
    return [
        AnalysisJudgment(
            claim_id=qualifying_claim.claim_id,
            instrument=qualifying_claim.tickers_affected[0],
            action_type=ActionType.BUY,
            description=f"Phase 4 stub: {qualifying_claim.claim[:100]}",
            group_id=None,
            one_sentence_thesis="Stub thesis — Phase 4 integration test.",
            expected_value=0.08,
            conviction=ConvictionLevel.MEDIUM,
            scenario_table=ScenarioTable(
                bull=Scenario.model_validate(
                    {"probability": 30, "return": 0.20, "timeframe": None, "confirming_metric": None, "mechanism": None, "max_drawdown": None}
                ),
                base=Scenario.model_validate(
                    {"probability": 50, "return": 0.08, "timeframe": None, "confirming_metric": None, "mechanism": None, "max_drawdown": None}
                ),
                bear=Scenario.model_validate(
                    {"probability": 20, "return": -0.10, "timeframe": None, "confirming_metric": None, "mechanism": None, "max_drawdown": None}
                ),
            ),
            invalidation_conditions=[],
            sizing_rationale="Phase 4 stub sizing.",
            step_failed=None,
        )
    ]


def _stub_behavioral_match(_step: ActionStep, _slug: str) -> bool:
    """Return True — all steps pass behavioral match in Phase 4."""
    return True


def _stub_place_order(params: ExecutionParameters) -> str:
    """Return a paper trade order ID — no real order placed in Phase 4."""
    return f"PAPER-{params.client_order_id}"


def _stub_send_email(_subject: str, _body: str) -> None:
    """No-op email stub — real sender wired in Phase 7."""


def run_pipeline(
    signals_dir: Path,
    *,
    run_dir: Path | None = None,
) -> PipelineState:
    """Execute the full pipeline on the signals in signals_dir and return the final state."""
    slug: str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    working_dir: Path = run_dir if run_dir is not None else Path("runs") / slug
    working_dir.mkdir(parents=True, exist_ok=True)

    signals_out: Path = working_dir / "signals"
    signals_out.mkdir(exist_ok=True)

    for signal_file in signals_dir.glob("*.json"):
        _ = shutil.copy2(signal_file, signals_out / signal_file.name)

    config = load_config()
    graph = build_graph(
        fetch_portfolio=_stub_fetch_portfolio,
        corroboration_agent=_stub_corroboration,
        claim_questions_agent=_stub_claim_questions,
        answer_synthesis_agent=_stub_answer_synthesis,
        config=config,
        thesis_agent=_stub_thesis_agent,
        behavioral_match_agent=_stub_behavioral_match,
        place_order=_stub_place_order,
        send_email=_stub_send_email,
    )

    initial_state: PipelineState = {
        "slug": slug,
        "working_dir": str(working_dir),
        "completed_steps": [],
    }

    result = graph.invoke(initial_state)
    return result  # type: ignore[return-value]  # pyright: ignore[reportReturnType]
