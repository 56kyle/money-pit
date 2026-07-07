"""A2 node: template emission, ID assignment, routing-table data_sources, file writes."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from loguru import logger

from money_pit.compute.routing import CATEGORY_TO_TOOLS
from money_pit.compute.signal_flags import count_by_tier
from money_pit.constants import AGGREGATED_SIGNALS_JSON_FILENAME
from money_pit.constants import INITIAL_QUESTIONS_JSON_FILENAME
from money_pit.constants import INITIAL_QUESTIONS_MD_FILENAME
from money_pit.constants import PORTFOLIO_SNAPSHOT_FILENAME
from money_pit.contracts import ClaimQuestionsAgent
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.macro import MACRO_INDICATOR_SERIES
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.questions import INDICATOR_PREFIX
from money_pit.schemas.questions import InitialQuestions
from money_pit.schemas.questions import Question
from money_pit.schemas.questions import SignalSummary
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.signals import Claim


_CLAIM_SUMMARY_MAX_LEN: int = 80

_MACRO_QUESTION_TEXT: dict[str, tuple[str, str]] = {
    "yield_curve": (
        "What is the current T10Y2Y 10-year minus 2-year Treasury yield spread in percentage points?",
        "Yield curve shape determines credit cycle phase.",
    ),
    "credit_spreads": (
        "What is the current ICE BofA US High Yield OAS credit spread in percentage points?",
        "Credit spread width signals financial stress.",
    ),
    "pmi": (
        "What is the current ISM Manufacturing PMI reading?",
        "PMI above/below 50 signals expansion/contraction.",
    ),
    "earnings_revisions": (
        "What is the current S&P 500 forward EPS breadth — fraction of constituents with upward revisions minus downward?",
        "Positive earnings revision breadth confirms growth acceleration.",
    ),
    "inflation": (
        "What is the most recent US CPI core (CPILFESL) year-over-year percentage change?",
        "Inflation above target constrains monetary easing.",
    ),
}


def _make_macro_questions() -> list[Question]:
    questions: list[Question] = []
    for indicator_name in MACRO_INDICATOR_SERIES:
        question, rationale = _MACRO_QUESTION_TEXT[indicator_name]
        questions.append(
            Question(
                id="",
                category=QuestionCategory.MACRO_REGIME,
                question=question,
                signal_source=f"{INDICATOR_PREFIX}{indicator_name}",
                signal_tier=SignalTier.LOW,
                rationale=rationale,
                data_sources=CATEGORY_TO_TOOLS[QuestionCategory.MACRO_REGIME],
                answer=None,
            )
        )
    return questions


def _make_current_events_questions(high_medium_claims: list[Claim]) -> list[Question]:
    questions: list[Question] = []
    for claim in high_medium_claims:
        published_at: str = claim.source_ref.published_at or "the source publication date"
        question_text: str = (
            f"What material developments have occurred regarding {claim.claim[:_CLAIM_SUMMARY_MAX_LEN]}"
            f" since {published_at}?"
        )
        questions.append(
            Question(
                id="",
                category=QuestionCategory.CURRENT_EVENTS,
                question=question_text,
                signal_source=claim.claim_id,
                signal_tier=claim.tier,
                rationale="Recency check on the thesis underpinning this signal.",
                data_sources=CATEGORY_TO_TOOLS[QuestionCategory.CURRENT_EVENTS],
                answer=None,
            )
        )
    return questions


def _make_portfolio_gap_questions(
    high_medium_claims: list[Claim],
    portfolio: PortfolioSnapshot,
) -> list[Question]:
    ticker_to_claims: dict[str, list[Claim]] = {}
    for claim in high_medium_claims:
        for ticker in claim.tickers_affected or []:
            ticker_to_claims.setdefault(ticker, []).append(claim)

    questions: list[Question] = []
    for position in portfolio.positions:
        if position.ticker not in ticker_to_claims:
            continue
        claims_for_ticker: list[Claim] = ticker_to_claims[position.ticker]
        claim_summary: str = claims_for_ticker[0].claim[:_CLAIM_SUMMARY_MAX_LEN]
        question_text: str = (
            f"How does the recent {claim_summary} interact with the current"
            f" {position.ticker} position"
            f" (cost basis {position.cost_basis}, {position.sector})?"
        )
        questions.append(
            Question(
                id="",
                category=QuestionCategory.PORTFOLIO_GAP,
                question=question_text,
                signal_source=position.ticker,
                signal_tier=SignalTier.PORTFOLIO,
                rationale="Portfolio-overlap check before sizing.",
                data_sources=CATEGORY_TO_TOOLS[QuestionCategory.PORTFOLIO_GAP],
                answer=None,
            )
        )

    if not questions:
        questions.append(
            Question(
                id="",
                category=QuestionCategory.PORTFOLIO_GAP,
                question="No portfolio overlap detected for current signals.",
                signal_source=None,
                signal_tier=SignalTier.PORTFOLIO,
                rationale="Placeholder — no actionable overlap.",
                data_sources=CATEGORY_TO_TOOLS[QuestionCategory.PORTFOLIO_GAP],
                answer=None,
            )
        )

    return questions


def _draft_to_question(draft: DraftQuestion, claims_by_id: dict[str, Claim]) -> Question | None:
    origin_claim: Claim | None = claims_by_id.get(draft.signal_source)
    if origin_claim is None:
        logger.warning(
            "Dropping A2 draft question referencing unknown claim {signal_source}",
            signal_source=draft.signal_source,
        )
        return None
    return Question(
        id="",
        category=draft.category,
        question=draft.question,
        signal_source=draft.signal_source,
        signal_tier=origin_claim.tier,
        rationale=draft.rationale,
        data_sources=CATEGORY_TO_TOOLS[draft.category],
        answer=None,
    )


def _assign_ids(questions: list[Question]) -> list[Question]:
    return [q.model_copy(update={"id": f"Q{i + 1:03d}"}) for i, q in enumerate(questions)]


def _render_markdown(result: InitialQuestions) -> str:
    summary: SignalSummary = result.signal_summary
    lines: list[str] = [
        f"# Initial Questions — {result.slug}",
        "",
        f"Generated: {result.generated_at}",
        "",
        "## Signal Summary",
        f"- High: {summary.high_signal_count}  Medium: {summary.medium_signal_count}  Low: {summary.low_signal_count}",
        f"- Questions generated: {summary.questions_generated}",
        "",
        "## Questions",
    ]
    for question in result.questions:
        tools_str: str = ", ".join(ds.value for ds in question.data_sources)
        lines += [
            "",
            f"### {question.id} — {question.category.value}",
            f"**{question.question}**",
            f"Source: {question.signal_source} | Tier: {question.signal_tier.value}",
            f"Tools: {tools_str}",
            f"Rationale: {question.rationale}",
        ]
    return "\n".join(lines) + "\n"


def _load_questions_inputs(working_dir: Path) -> tuple[AggregatedSignals, PortfolioSnapshot]:
    """Load aggregated signals and portfolio snapshot from the working directory."""
    aggregated_signals: AggregatedSignals = AggregatedSignals.model_validate_json(
        (working_dir / AGGREGATED_SIGNALS_JSON_FILENAME).read_text(encoding="utf-8")
    )
    portfolio: PortfolioSnapshot = PortfolioSnapshot.model_validate_json(
        (working_dir / PORTFOLIO_SNAPSHOT_FILENAME).read_text(encoding="utf-8")
    )
    return aggregated_signals, portfolio


def _make_llm_questions(
    claim_questions_agent: ClaimQuestionsAgent,
    high_medium_claims: list[Claim],
) -> list[Question]:
    """Return A2 agent-authored questions, soft-failing to an empty list on agent error."""
    try:
        drafts: list[DraftQuestion] = claim_questions_agent(high_medium_claims)
    except Exception:
        logger.exception("A2 claim questions agent failed; proceeding with no LLM-authored questions")
        drafts = []

    claims_by_id: dict[str, Claim] = {c.claim_id: c for c in high_medium_claims}
    return [
        question
        for question in (_draft_to_question(draft, claims_by_id) for draft in drafts)
        if question is not None
    ]


def _build_signal_summary(
    aggregated_signals: AggregatedSignals,
    questions_generated: int,
) -> SignalSummary:
    """Build the signal-tier summary from claim tier counts and the total question count."""
    tier_counts: dict[SignalTier, int] = count_by_tier(aggregated_signals.claims)
    return SignalSummary(
        high_signal_count=tier_counts[SignalTier.HIGH],
        medium_signal_count=tier_counts[SignalTier.MEDIUM],
        low_signal_count=tier_counts[SignalTier.LOW],
        questions_generated=questions_generated,
    )


def _write_questions_outputs(working_dir: Path, initial_questions: InitialQuestions) -> None:
    """Write the initial-questions JSON and markdown artifacts to the working directory."""
    _ = (working_dir / INITIAL_QUESTIONS_JSON_FILENAME).write_text(
        initial_questions.model_dump_json(indent=2),
        encoding="utf-8",
    )
    _ = (working_dir / INITIAL_QUESTIONS_MD_FILENAME).write_text(
        _render_markdown(initial_questions),
        encoding="utf-8",
    )


def make_questions_node(
    claim_questions_agent: ClaimQuestionsAgent,
) -> PipelineNode:
    """Return a LangGraph node that generates initial research questions."""

    def questions_node(state: PipelineState) -> PipelineState:
        slug: str = require_slug(state)
        working_dir: Path = require_working_dir(state)

        aggregated_signals, portfolio = _load_questions_inputs(working_dir)

        high_medium_claims: list[Claim] = [
            c for c in aggregated_signals.claims if c.tier in (SignalTier.HIGH, SignalTier.MEDIUM)
        ]

        macro_qs: list[Question] = _make_macro_questions()
        current_events_qs: list[Question] = _make_current_events_questions(high_medium_claims)
        portfolio_gap_qs: list[Question] = _make_portfolio_gap_questions(high_medium_claims, portfolio)
        llm_qs: list[Question] = _make_llm_questions(claim_questions_agent, high_medium_claims)

        all_questions: list[Question] = _assign_ids(macro_qs + current_events_qs + portfolio_gap_qs + llm_qs)

        signal_summary: SignalSummary = _build_signal_summary(aggregated_signals, len(all_questions))

        generated_at: str = datetime.now(timezone.utc).isoformat()
        initial_questions: InitialQuestions = InitialQuestions(
            slug=slug,
            generated_at=generated_at,
            signal_summary=signal_summary,
            questions=all_questions,
            error=None,
        )

        _write_questions_outputs(working_dir, initial_questions)

        return {"completed_steps": with_completed_step(state, "questions")}

    return questions_node
